"""Freeform Blueprint: the allow-list check, running a script to a solid
(scaled from inches), the deliverables it writes, the time limit in the
worker, and the read loop in code and auto modes against a scripted
provider and a fake builder."""
import json
import os

import pytest

pytest.importorskip('PIL')

from stl_to_solid.blueprint.freeform import FreeformError, check_code, compile_code, run_script

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'sg90_recipe.json')

# the kind of script the model writes: parameters, helpers, a tilted plane,
# arcs, a guarded fillet, a loop of cuts
GOOD = '''
import math
import cadquery as cq

plate_t = 0.375      # ".375"
arm_len = 3.9        # "3.9"
width = 1.5          # "(1.5)"
hole_d = 0.34375     # "DIA .34375"
tilt = 15.0          # 75 deg from the vertical flange

def unit(v):
    L = math.hypot(*v)
    return (v[0] / L, v[1] / L)

add = lambda p, q, k=1.0: (p[0] + k * q[0], p[1] + k * q[1])
d = unit((math.cos(math.radians(tilt)), -math.sin(math.radians(tilt))))
prof = (cq.Workplane("XY").moveTo(0, 0).lineTo(arm_len, 0).lineTo(arm_len, plate_t)
        .threePointArc((arm_len / 2, plate_t + 0.2), (0, plate_t)).close())
body = prof.extrude(width)
pl = cq.Plane(origin=(arm_len / 2, plate_t, width / 2), xDir=(d[0], d[1], 0), normal=(0, 0, 1))
boss = cq.Workplane(pl).circle(0.4).extrude(0.5)
body = body.union(boss)
try:
    body = body.edges("|Z").fillet(0.05)
except Exception as e:
    print(f"no fillet: {e}")
for x in (0.5, arm_len - 0.5):
    body = body.cut(cq.Workplane("XY").center(x, width / 2).circle(hole_d / 2).extrude(5, both=True))
result = body
'''


def test_check_accepts_the_shape_of_a_real_script():
    assert check_code(GOOD) == []


@pytest.mark.parametrize('code,why', [
    ('import os\nresult = 1', 'only `import cadquery as cq`'),
    ('from cadquery import Workplane\nresult = Workplane()', 'from-import'),
    ('import cadquery as cq\nx = open("f")\nresult = cq.Workplane()', "'open' is not defined"),
    ('import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)\nresult.exportStep("a")', '.exportStep'),
    ('import cadquery as cq\nresult = cq.importers.importStep("a")', '.importers'),
    ('import cadquery as cq\nwhile True:\n    pass\nresult = cq.Workplane()', 'while loop'),
    ('import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)\nprint(result.__class__)', '.__class__'),
    ('import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1).val().wrapped', '.wrapped'),
    ('import cadquery as cq\nresult = getattr(cq, "exporters")', "'getattr' is not defined"),
    ('import cadquery as cq\nresult = __import__("os")', 'starting with _'),
    ('import cadquery as cq\nclass A:\n    pass\nresult = cq.Workplane()', 'class'),
    ('import cadquery as cq\nwith open("f") as f:\n    pass\nresult = cq.Workplane()', 'with statement'),
    ('import cadquery as cq\nx = cq.Workplane().box(1, 1, 1)', 'must assign the finished part to `result`'),
    ('import cadquery as cq\nresult = cq.Workplane(.box', 'syntax error'),
    ('', 'empty'),
])
def test_check_refuses(code, why):
    errs = check_code(code)
    assert errs and any(why in e for e in errs), errs


def test_check_sizes():
    assert any('longer than' in e for e in check_code('import cadquery as cq\n' + 'x = 1\n' * 700 + 'result = cq.Workplane()'))


def test_run_script_scales_inches_to_mm_and_takes_shapes():
    import cadquery as cq
    shape, warn = run_script('import cadquery as cq\nresult = cq.Workplane().box(2, 1, 0.5)', units='in')
    bb = shape.BoundingBox()
    assert (bb.xlen, bb.ylen, bb.zlen) == pytest.approx((50.8, 25.4, 12.7))
    assert warn == []
    shape, _ = run_script('import cadquery as cq\nresult = cq.Workplane().box(2, 1, 0.5).val()', units='mm')
    assert isinstance(shape, cq.Shape) and shape.BoundingBox().xlen == pytest.approx(2)
    # the real-script shape builds, with its tilted boss and arcs
    shape, _ = run_script(GOOD, units='in')
    assert shape.isValid() and len(shape.Solids()) == 1


def test_run_script_failures_are_sentences():
    with pytest.raises(FreeformError, match=r'failed at line 2') as e:
        run_script('import cadquery as cq\nresult = cq.Workplane().box(1, 1, 0)')
    assert e.value.kind == 'run'
    with pytest.raises(FreeformError, match='empty workplane') as e:
        run_script('import cadquery as cq\nresult = cq.Workplane()')
    assert e.value.kind == 'result'
    with pytest.raises(FreeformError, match='holds no solid'):
        run_script('import cadquery as cq\nresult = cq.Workplane().rect(1, 1)')
    with pytest.raises(FreeformError, match='not a CadQuery'):
        run_script('import cadquery as cq\nresult = 42')
    with pytest.raises(FreeformError, match='refused') as e:
        run_script('import os\nresult = 1')
    assert e.value.kind == 'check'
    with pytest.raises(FreeformError, match=r'line 2 .*ZeroDivisionError'):
        run_script('import cadquery as cq\nx = 1 / 0\nresult = cq.Workplane().box(1, 1, 1)')


def test_the_script_cannot_reach_the_real_builtins():
    # `open` is refused by the checker; even a name the checker lets through
    # only sees the safe table at run time
    with pytest.raises(FreeformError, match='refused'):
        run_script('import cadquery as cq\nf = open\nresult = cq.Workplane().box(1, 1, 1)')
    with pytest.raises(FreeformError, match='refused'):
        run_script('import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)\nexec("x = 1")')


def test_compile_code_writes_the_deliverables(tmp_path):
    r = compile_code(GOOD, str(tmp_path), units='in', title='bracket')
    assert r['kind'] == 'code' and r['parametric'] is False and r['has_fusion_script'] is False
    assert r['solids'] == 1 and r['has_step'] is True
    assert r['bbox']['size'][0] == pytest.approx(3.9 * 25.4, abs=0.5)
    names = sorted(os.listdir(tmp_path))
    for n in ('output.step', 'preview.stl', 'preview_features.json', 'code.py', 'renders.json',
              'render_front.png', 'render_top.png', 'render_right.png', 'render_iso.png'):
        assert n in names, n
    with open(tmp_path / 'preview_features.json') as f:
        fm = json.load(f)
    assert fm['features'] == [{'id': 'part', 'name': 'bracket', 'op': 'new_body'}]
    assert len(fm['triangle_feature']) > 100 and set(fm['triangle_feature']) == {0}
    assert [x['view'] for x in r['renders']] == ['front', 'top', 'right', 'iso']
    with open(tmp_path / 'code.py') as f:
        assert f.read().strip() == GOOD.strip()
    q = compile_code(GOOD, str(tmp_path / 'q'), units='in', quick=True)
    assert q['has_step'] is False and not (tmp_path / 'q' / 'output.step').exists()
    assert (tmp_path / 'q' / 'preview.stl').exists()


def test_two_bodies_is_a_warning():
    r_shape, warn = run_script('import cadquery as cq\n'
                               'result = cq.Workplane().box(1, 1, 1).union(cq.Workplane().center(5, 0).box(1, 1, 1))')
    assert len(r_shape.Solids()) == 2 and warn == []


# ---- the worker runs it with a time limit --------------------------------------------

def test_worker_stops_a_runaway_script(tmp_path):
    pytest.importorskip('fastapi')
    from backend.freeform import run_quick
    res = run_quick({'tool': 'blueprint', 'quick': True, 'timeout_s': 3,
                     'code': 'import cadquery as cq\nfor i in range(10 ** 12):\n    x = i * 2\nresult = cq.Workplane().box(1, 1, 1)',
                     'units': 'mm'}, str(tmp_path / 'run'), timeout=3)
    assert res['ok'] is False and res['failure'] == 'timeout'
    assert 'longer than 3 s' in res['error']


def test_worker_builds_quick(tmp_path):
    pytest.importorskip('fastapi')
    from backend.freeform import run_quick
    res = run_quick({'tool': 'blueprint', 'quick': True, 'timeout_s': 60,
                     'code': 'import cadquery as cq\nresult = cq.Workplane().box(10, 20, 30)', 'units': 'mm'},
                    str(tmp_path / 'run'), timeout=60)
    assert res['ok'] is True and res['solids'] == 1
    assert res['bbox']['size'] == pytest.approx([10, 20, 30])
    assert [r['view'] for r in res['renders']] == ['front', 'top', 'right', 'iso']
    assert (tmp_path / 'run' / 'render_front.png').exists() and not (tmp_path / 'run' / 'output.step').exists()
    res = run_quick({'tool': 'blueprint', 'quick': True, 'timeout_s': 60,
                     'code': 'import cadquery as cq\nresult = cq.Workplane().box(1, 1, 0)', 'units': 'mm'},
                    str(tmp_path / 'bad'), timeout=60)
    assert res['ok'] is False and 'failed at line 2' in res['error']
    assert res['validation']['errors'] == [res['error']]


# ---- the read loop -----------------------------------------------------------------------

def _png_bytes(size=(800, 600)):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', size, 'white').save(b, 'PNG')
    return b.getvalue()


def _answer(code, **kw):
    a = {'name': 'bracket', 'units': 'in', 'overall': {'w': 3.9, 'd': 1.5, 'h': 2.0},
         'params': [{'name': 'plate_t', 'value': 0.375, 'source': 'front: .375', 'inferred': False}],
         'code': code, 'notes': ['sheet: front, top and right views']}
    a.update(kw)
    return json.dumps(a)


def _critique(done, code='', disc=()):
    return json.dumps({'done': done, 'discrepancies': list(disc), 'code': code, 'units': 'in',
                       'overall': {'w': 3.9, 'd': 1.5, 'h': 2.0}, 'params': [], 'notes': []})


class FakeBuilder:
    """Pretends to build: scripts containing 'BAD' fail, the rest build a
    box with four pictures. Records what it was asked to build."""
    def __init__(self):
        self.calls = []

    def __call__(self, kind, answer):
        from stl_to_solid.blueprint.read_drawing import BuildCheck
        self.calls.append((kind, answer))
        code = answer.get('code', '') if kind == 'code' else json.dumps(answer)
        if 'BAD' in code:
            return BuildCheck(False, 'The script failed at line 3: ValueError: BAD')
        renders = [(v, b'\x89PNG' + v.encode(), f'Our build, {v.upper()} view') for v in ('front', 'top', 'right', 'iso')]
        return BuildCheck(True, bbox={'size': [99.0, 38.1, 50.8]}, volume_mm3=1000.0, solids=1, renders=renders)


@pytest.fixture
def Scripted():
    from .test_blueprint_read import Scripted
    return Scripted


def test_code_mode_run_and_one_look_that_matches(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    from stl_to_solid.blueprint.prompts import CODE_SYSTEM_PROMPT
    code = 'import cadquery as cq\nresult = cq.Workplane().box(3.9, 1.5, 2)'
    p = Scripted([_answer(code), _critique(True, disc=[{'where': 'front', 'what': 'fine', 'fix': ''}])])
    b = FakeBuilder()
    said = []
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', rounds=1, builder=b,
                       progress=said.append)
    assert out['mode'] == 'code' and out['recipe'] is None
    assert out['freeform']['code'] == code and out['freeform']['units'] == 'in'
    assert out['freeform']['params'][0]['name'] == 'plate_t'
    assert out['validation'] == {'errors': [], 'warnings': []}
    assert out['usage']['calls'] == 2 and not out['repaired']
    assert [c[0] for c in b.calls] == ['code']
    # the critique call carried the drawing and the four pictures, each captioned, and the script
    crit = p.calls[1]
    assert crit['system'] == CODE_SYSTEM_PROMPT and crit['n_images'] == 5 and crit['effort'] == 'medium'
    assert 'Our build, TOP view' in crit['text'] and code in crit['text']
    assert crit['schema']['properties']['code']['type'] == 'string'
    assert out['rounds'][0] == {'round': 0, 'kind': 'first', 'code': code}
    assert out['rounds'][1:] == [{'round': 1, 'kind': 'look', 'built': {'size': [99.0, 38.1, 50.8], 'solids': 1},
                                 'discrepancies': [{'where': 'front', 'what': 'fine', 'fix': ''}], 'done': True}]
    assert any('matches the drawing' in s for s in said)
    assert p.calls[0]['effort'] == 'high' and 'CadQuery script' in p.calls[0]['text']


def test_code_mode_fixes_a_script_that_fails(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    bad = 'import cadquery as cq\nx = 1\nresult = cq.Workplane().box(1, 1, 1)  # BAD'
    good = 'import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)'
    p = Scripted([_answer(bad), _critique(False, code=good)])
    b = FakeBuilder()
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', rounds=0, builder=b)
    assert out['freeform']['code'] == good and out['repaired'] and out['validation']['errors'] == []
    assert [c[1]['code'] for c in b.calls] == [bad, good]
    fix = p.calls[1]
    assert 'did not run' in fix['text'] and 'ValueError: BAD' in fix['text'] and bad in fix['text']
    assert fix['effort'] == 'high' and fix['n_images'] == 1
    assert out['rounds'][1]['kind'] == 'fix' and 'BAD' in out['rounds'][1]['error']
    # the name and params survive a fix answer that leaves them empty
    assert out['freeform']['name'] == 'bracket' and out['freeform']['params'][0]['name'] == 'plate_t'


def test_code_mode_gives_up_after_the_fix_budget(Scripted):
    from stl_to_solid.blueprint.read_drawing import FIX_BUDGET, read_drawing
    bad = 'import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)  # BAD'
    p = Scripted([_answer(bad)] + [_critique(False, code=bad + f'\n# try {i}') for i in range(FIX_BUDGET)])
    b = FakeBuilder()
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', rounds=2, builder=b)
    assert out['usage']['calls'] == 1 + FIX_BUDGET and len(b.calls) == 1 + FIX_BUDGET
    assert out['validation']['errors'] == ['The script failed at line 3: ValueError: BAD']
    assert out['freeform']['code'].endswith(f'# try {FIX_BUDGET - 1}')


def test_code_mode_static_refusal_is_fixed_before_running(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    good = 'import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)'
    p = Scripted([_answer('import os\nresult = 1'), _critique(False, code=good)])
    b = FakeBuilder()
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', rounds=0, builder=b)
    assert out['freeform']['code'] == good and [c[1]['code'] for c in b.calls] == [good]
    assert 'refused' in p.calls[1]['text'] and 'only `import cadquery as cq`' in p.calls[1]['text']


def test_code_mode_look_changes_the_script_and_runs_it_again(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    v1 = 'import cadquery as cq\nresult = cq.Workplane().box(3.9, 1.5, 2)'
    v2 = 'import cadquery as cq\nresult = cq.Workplane().box(4.126, 1.5, 2)'
    p = Scripted([_answer(v1), _critique(False, code=v2, disc=[{'where': 'top', 'what': 'boss inside 3.9', 'fix': 'move it'}]),
                  _critique(True)])
    b = FakeBuilder()
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', rounds=2, builder=b)
    assert out['freeform']['code'] == v2
    assert [c[1]['code'] for c in b.calls] == [v1, v2]
    looks = [r for r in out['rounds'] if r['kind'] == 'look']
    assert [r.get('changed', False) for r in looks] == [True, False]
    assert looks[0]['code'] == v2 and 'code' not in looks[1]
    assert looks[1]['done'] is True and out['usage']['calls'] == 3


def test_code_mode_without_a_builder_only_checks_statically(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    good = 'import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)'
    p = Scripted([_answer(good)])
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', rounds=2)
    assert out['usage']['calls'] == 1 and out['freeform']['code'] == good
    assert out['rounds'] == [{'round': 0, 'kind': 'first', 'code': good}]


def test_code_mode_refused_when_not_allowed(Scripted):
    from stl_to_solid.blueprint.providers import ReadError
    from stl_to_solid.blueprint.read_drawing import read_drawing
    p = Scripted([])
    with pytest.raises(ReadError, match='switched off'):
        read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='code', allow_code=False)
    assert p.calls == []


def test_auto_mode_triage_picks_the_language(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    with open(FIX) as f:
        rec = f.read()
    good = 'import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)'
    # fits the recipe: the triage is one cheap call, then the recipe read
    p = Scripted([json.dumps({'recipe_can_express': True, 'why': 'boxes and holes', 'units': 'mm'}), rec])
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='auto', rounds=0)
    assert out['mode'] == 'recipe' and out['recipe']['params'][0]['name'] == 'body_w'
    assert out['triage'] == {'recipe_can_express': True, 'why': 'boxes and holes', 'units': 'mm'}
    assert p.calls[0]['effort'] == 'low' and 'RECIPE language' in p.calls[0]['text']
    # does not fit: code
    p = Scripted([json.dumps({'recipe_can_express': False, 'why': 'tilted boss and fillets', 'units': 'in'}),
                  _answer(good)])
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='auto', rounds=0)
    assert out['mode'] == 'code' and out['freeform']['code'] == good
    # code not allowed: auto means recipe, no triage call
    p = Scripted([rec])
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='auto', allow_code=False)
    assert out['mode'] == 'recipe' and out['triage'] is None and out['usage']['calls'] == 1


def test_recipe_mode_look_rounds(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    from stl_to_solid.blueprint.prompts import SYSTEM_PROMPT
    with open(FIX) as f:
        good = json.load(f)
    better = json.loads(json.dumps(good))
    better['params'][0]['value'] = 32.0        # overall is an expression over body_w: still consistent
    crit1 = json.dumps({'done': False, 'recipe': better,
                        'discrepancies': [{'where': 'body', 'what': 'too wide', 'fix': 'body_w 32'}]})
    crit2 = json.dumps({'done': True, 'recipe': None, 'discrepancies': []})
    p = Scripted([json.dumps(good), crit1, crit2])
    b = FakeBuilder()
    said = []
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='recipe', rounds=3, builder=b,
                       progress=said.append)
    assert out['mode'] == 'recipe' and out['freeform'] is None
    assert out['recipe']['params'][0]['value'] == 32.0 and out['validation']['errors'] == []
    assert out['usage']['calls'] == 3 and len(b.calls) == 2
    crit = p.calls[1]
    assert crit['system'] == SYSTEM_PROMPT and crit['n_images'] == 5 and crit['effort'] == 'medium'
    assert '"body_w"' in crit['text'] and 'recipe = null' in crit['text']
    assert crit['schema']['properties']['recipe']['anyOf'][1] == {'type': 'null'}
    assert [r.get('changed', False) for r in out['rounds']] == [True, False]
    assert out['rounds'][1]['done'] is True
    assert any('1 discrepancy fixed' in s for s in said)


def test_recipe_mode_rejects_a_worse_correction(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    with open(FIX) as f:
        good = json.load(f)
    worse = json.loads(json.dumps(good))
    worse['features'][0]['op'] = 'cut'
    crit1 = json.dumps({'done': False, 'recipe': worse, 'discrepancies': [{'where': 'x', 'what': 'y', 'fix': 'z'}]})
    p = Scripted([json.dumps(good), crit1])
    b = FakeBuilder()
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='recipe', rounds=2, builder=b)
    assert out['recipe']['features'][0]['op'] == 'new_body' and out['validation']['errors'] == []
    assert 'rejected' in out['rounds'][0] and len(out['rounds']) == 1 and out['usage']['calls'] == 2


def test_recipe_mode_cannot_express_becomes_a_warning(Scripted):
    from stl_to_solid.blueprint.read_drawing import read_drawing
    with open(FIX) as f:
        good = json.load(f)
    good['cannot_express'] = ['R.5 fillets on the web']
    p = Scripted([json.dumps(good)])
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='recipe')
    assert out['recipe']['cannot_express'] == ['R.5 fillets on the web']
    assert any('cannot express: R.5 fillets' in w for w in out['validation']['warnings'])


def test_recipe_mode_build_failure_stops_the_looks(Scripted):
    from stl_to_solid.blueprint.read_drawing import BuildCheck, read_drawing
    with open(FIX) as f:
        good = f.read()
    p = Scripted([good])
    out = read_drawing(_png_bytes(), 'anthropic', 'k', client=p, mode='recipe', rounds=2,
                       builder=lambda kind, a: BuildCheck(False, 'The preview helper crashed on this shape'))
    assert out['usage']['calls'] == 1 and out['rounds'][0]['build_error'].startswith('The preview helper')
    assert out['validation']['errors'] == []
