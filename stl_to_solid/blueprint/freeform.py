"""Freeform Blueprint: the model writes a short CadQuery script instead of
a recipe, for parts the recipe language cannot draw (a boss square to a
sloped arm, arcs inside a profile, edge fillets, a revolved cone). The
script is checked, run, and its `result` becomes the STEP and preview;
there is no Fusion script, since code is not a list of sketches.

The check is an allow-list, not a sandbox: only `import cadquery` and
`import math`; a fixed set of statement and expression kinds (no while,
with, class, global, from-import, yield, del); every free name must be
one the script binds or one of a few safe builtins (so open, exec, eval,
getattr, type, __import__ are unreachable by not being there); no name
or attribute starting with `_`, and none of the kernel's file surfaces
(importers, exporters, export*, import*, save, load, wrapped, occ_impl).
The script then runs with that builtins table and nothing else. It stops
accidents and the obvious tricks; a process of its own with a wall-clock
limit (backend.worker) and the container's memory limit do the rest.
"""
import ast
import builtins
import importlib
import json
import os
import traceback

MAX_LINES = 600
MAX_CHARS = 60000
ALLOWED_MODULES = {'cadquery': 'cq', 'math': 'math'}
SAFE_BUILTIN_NAMES = (
    'abs', 'min', 'max', 'round', 'range', 'len', 'float', 'int', 'str', 'bool', 'list', 'tuple',
    'dict', 'set', 'enumerate', 'zip', 'sorted', 'reversed', 'sum', 'any', 'all', 'map', 'filter',
    'print', 'isinstance', 'pow', 'divmod', 'repr', 'Exception', 'ValueError', 'RuntimeError',
    'TypeError', 'ZeroDivisionError', 'AssertionError', 'ArithmeticError', 'IndexError', 'KeyError',
    'True', 'False', 'None',
)
BLOCKED_ATTRS = {'importers', 'exporters', 'occ_impl', 'wrapped', 'save', 'load', 'open', 'read',
                 'write', 'system', 'popen', 'environ', 'path', 'builtins', 'globals', 'locals'}
BLOCKED_ATTR_PREFIXES = ('_', 'export', 'import', 'save', 'load', 'to_file', 'tofile', 'fromfile')
_ALLOWED_NODES = {
    'Module', 'Expr', 'Assign', 'AugAssign', 'AnnAssign', 'For', 'If', 'IfExp', 'FunctionDef',
    'Return', 'Lambda', 'Call', 'Attribute', 'Name', 'Constant', 'BinOp', 'UnaryOp', 'BoolOp',
    'Compare', 'Subscript', 'Slice', 'Tuple', 'List', 'Dict', 'Set', 'ListComp', 'GeneratorExp',
    'DictComp', 'SetComp', 'comprehension', 'Starred', 'keyword', 'arg', 'arguments', 'Import',
    'alias', 'Pass', 'Break', 'Continue', 'Assert', 'Try', 'ExceptHandler', 'Raise', 'JoinedStr',
    'FormattedValue', 'Load', 'Store', 'Del',
    # operators and contexts
    'Add', 'Sub', 'Mult', 'Div', 'FloorDiv', 'Mod', 'Pow', 'USub', 'UAdd', 'Not', 'Invert', 'And',
    'Or', 'Eq', 'NotEq', 'Lt', 'LtE', 'Gt', 'GtE', 'Is', 'IsNot', 'In', 'NotIn', 'BitAnd', 'BitOr',
    'BitXor', 'LShift', 'RShift', 'MatMult',
}
_WHY = {
    'While': 'a while loop (use for over a range, so it always ends)',
    'With': 'a with statement', 'ClassDef': 'a class', 'Global': 'global', 'Nonlocal': 'nonlocal',
    'ImportFrom': 'from-import (write `import cadquery as cq`)', 'Yield': 'yield', 'YieldFrom': 'yield',
    'Await': 'await', 'AsyncFor': 'async', 'AsyncWith': 'async', 'AsyncFunctionDef': 'async def',
    'Delete': 'del', 'NamedExpr': 'the walrus operator', 'Match': 'match',
}


class FreeformError(RuntimeError):
    """A failure with a sentence for the user and the model (errors.describe
    returns str(exc) as is). `kind`: 'check' (refused before running),
    'run' (the script raised), 'result' (ran, but `result` is no solid)."""
    readable = True

    def __init__(self, message, kind='run'):
        super().__init__(message)
        self.kind = kind


def _bound_names(tree):
    """Every name the script binds anywhere (assignment targets, loop and
    comprehension targets, function and argument names, imports, except
    names). Scope is ignored on purpose: the point is to refuse names the
    script never defines, not to typecheck it."""
    names = set()

    def target(t):
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                target(t)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.comprehension)):
            target(node.target)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split('.')[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def check_code(code):
    """The reasons this script is refused, as sentences; [] when it may run."""
    errors = []
    if not isinstance(code, str) or not code.strip():
        return ['the script is empty']
    if len(code) > MAX_CHARS or code.count('\n') + 1 > MAX_LINES:
        errors.append(f'the script is longer than {MAX_LINES} lines / {MAX_CHARS} characters')
    try:
        tree = ast.parse(code, '<drawing>', 'exec')
    except SyntaxError as e:
        return errors + [f'line {e.lineno}: syntax error: {e.msg}']
    seen = set()
    for node in ast.walk(tree):
        kind = type(node).__name__
        if kind not in _ALLOWED_NODES:
            why = _WHY.get(kind, kind)
            msg = f'line {getattr(node, "lineno", "?")}: {why} is not allowed'
            if msg not in seen:
                seen.add(msg)
                errors.append(msg)
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_MODULES:
                    errors.append(f'line {node.lineno}: only `import cadquery as cq` and `import math` '
                                  f'are allowed (not {a.name})')
                elif a.asname and a.asname.startswith('_'):
                    errors.append(f'line {node.lineno}: an alias starting with _ is not allowed')
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a in BLOCKED_ATTRS or a.startswith(BLOCKED_ATTR_PREFIXES):
                errors.append(f'line {node.lineno}: the attribute .{a} is not allowed '
                              '(no file access, no private kernel objects)')
        elif isinstance(node, ast.Name):
            if node.id.startswith('_'):
                errors.append(f'line {node.lineno}: names starting with _ are not allowed ({node.id})')
        elif isinstance(node, ast.arg) and node.arg.startswith('_'):
            errors.append(f'line {node.lineno}: names starting with _ are not allowed ({node.arg})')
        elif isinstance(node, ast.FunctionDef) and node.name.startswith('_'):
            errors.append(f'line {node.lineno}: names starting with _ are not allowed ({node.name})')
    bound = _bound_names(tree)
    safe = set(SAFE_BUILTIN_NAMES)
    free = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound and node.id not in safe:
                free.setdefault(node.id, node.lineno)
    for name, line in sorted(free.items(), key=lambda kv: kv[1]):
        errors.append(f'line {line}: the name {name!r} is not defined in the script and is not one of '
                      'the allowed builtins')
    if not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'result' for t in n.targets)
               for n in tree.body):
        errors.append('the script must assign the finished part to `result` at the top level')
    return errors


def _safe_globals(tree):
    """The script's globals: the safe builtins, and the allowed modules
    bound under the script's own aliases, with the Import statements
    removed (there is no __import__ for them to use)."""
    table = {n: getattr(builtins, n) for n in SAFE_BUILTIN_NAMES if hasattr(builtins, n)}
    g = {'__builtins__': table, '__name__': '__drawing__'}
    body = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                g[a.asname or a.name] = importlib.import_module(a.name)
        else:
            body.append(node)
    for node in ast.walk(tree):            # imports inside functions, the same way
        if isinstance(node, ast.Import) and node not in tree.body:
            for a in node.names:
                g[a.asname or a.name] = importlib.import_module(a.name)
    tree.body = body
    return g


def _strip_nested_imports(tree):
    for node in ast.walk(tree):
        for field in ('body', 'orelse', 'finalbody'):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list):
                kept = [s for s in stmts if not isinstance(s, ast.Import)]
                if len(kept) != len(stmts):
                    setattr(node, field, kept or [ast.Pass()])
    return ast.fix_missing_locations(tree)


def _where(exc):
    """'line N (`source`)' for the script frame that raised, or ''."""
    for fr in reversed(traceback.extract_tb(exc.__traceback__)):
        if fr.filename == '<drawing>':
            return f'line {fr.lineno}'
    return ''


def run_script(code, units='mm'):
    """Check and run the script; (cq.Shape in mm, warnings). Raises
    FreeformError with a sentence that names the line."""
    errors = check_code(code)
    if errors:
        raise FreeformError('The script was refused: ' + '; '.join(errors[:6]), 'check')
    tree = ast.parse(code, '<drawing>', 'exec')
    g = _safe_globals(tree)
    tree = _strip_nested_imports(tree)
    lines = code.splitlines()
    try:
        exec(compile(tree, '<drawing>', 'exec'), g)       # noqa: S102 - the allow-list above is the guard
    except Exception as e:                                 # noqa: BLE001 - every failure must be a sentence
        where = _where(e)
        src = ''
        try:
            n = int(where.split()[1])
            src = ' (`' + lines[n - 1].strip() + '`)'
        except (IndexError, ValueError):
            pass
        msg = str(e).strip() or type(e).__name__
        raise FreeformError(f'The script failed at {where or "an unknown line"}{src}: '
                            f'{type(e).__name__}: {msg}', 'run') from e
    return _take_result(g.get('result'), units)


def _take_result(r, units):
    import cadquery as cq
    warnings = []
    if r is None:
        raise FreeformError('The script did not assign `result`', 'result')
    if isinstance(r, cq.Workplane):
        shapes = [v for v in r.vals() if isinstance(v, cq.Shape)]
        if not shapes:
            raise FreeformError('The script\'s `result` is an empty workplane: nothing was built on it', 'result')
        shape = shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
    elif isinstance(r, cq.Shape):
        shape = r
    else:
        raise FreeformError(f'The script\'s `result` is a {type(r).__name__}, not a CadQuery Workplane or Shape', 'result')
    solids = shape.Solids()
    if not solids:
        raise FreeformError('The script\'s `result` holds no solid (a face, wire or sketch was built, not a body); '
                            'extrude or revolve it', 'result')
    shape = solids[0] if len(solids) == 1 else cq.Compound.makeCompound(solids)
    if not shape.isValid():
        fixed = shape.fix()
        if fixed.isValid():
            shape = fixed
            warnings.append('the solid needed repairing (overlapping or zero-thickness geometry); check it')
        else:
            raise FreeformError('The script\'s result is not a valid solid: a boolean left overlapping or '
                                'zero-thickness geometry. Check that every piece overlaps its '
                                'neighbour by a clear margin and that no cut is tangent to a face.', 'result')
    if units == 'in':
        shape = shape.scale(25.4)
    return shape, warnings


def compile_code(code, out_dir, units='mm', title=None, stem='output', quick=False):
    """Run the script and write the deliverables next to the recipe path's:
    <stem>.step (not when quick), preview.stl, preview_features.json (one
    feature, so the viewer colours it), render_<view>.png + renders.json,
    and code.py (the script as run). Returns the summary dict the worker
    puts in result.json. Raises FreeformError."""
    import cadquery as cq
    from ..pipeline import accurate_volume, tessellate_solid
    from .build_cq import PREVIEW_ANG, PREVIEW_TOL
    from .render import coarse_mesh, render_views, write_renders
    shape, warnings = run_script(code, units)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'code.py'), 'w', encoding='utf-8') as f:
        f.write(code if code.endswith('\n') else code + '\n')
    name = title or 'Blueprint'
    solids = shape.Solids()
    wp = cq.Workplane().add(shape)
    bb = shape.BoundingBox()
    lo, hi = [bb.xmin, bb.ymin, bb.zmin], [bb.xmax, bb.ymax, bb.zmax]
    bbox = {'min': lo, 'max': hi, 'size': [hi[i] - lo[i] for i in range(3)]}
    try:
        volume = accurate_volume(shape)
    except Exception:
        volume = 0.0
    if not quick:
        from ..rebuild import write_step
        write_step([s.wrapped for s in solids], os.path.join(out_dir, f'{stem}.step'), names=[name] * len(solids))
    mesh = tessellate_solid(wp, PREVIEW_TOL, PREVIEW_ANG)
    mesh.export(os.path.join(out_dir, 'preview.stl'))
    with open(os.path.join(out_dir, 'preview_features.json'), 'w') as f:
        json.dump({'features': [{'id': 'part', 'name': name, 'op': 'new_body'}],
                   'triangle_feature': [0] * len(mesh.faces)}, f)
    renders = write_renders(render_views(coarse_mesh(wp)), out_dir)
    if len(solids) > 1:
        warnings.append(f'the script built {len(solids)} separate bodies; if the drawing shows one part, '
                        'a piece is not touching its neighbour')
    return {
        'mode': 'blueprint', 'kind': 'code', 'parametric': False,
        'warnings': warnings, 'errors': [],
        'bbox': bbox, 'volume_mm3': float(volume), 'solids': len(solids), 'faces': len(shape.Faces()),
        'has_fusion_script': False, 'has_step': not quick, 'preview': 'preview.stl',
        'preview_map': 'preview_features.json', 'renders': renders,
    }
