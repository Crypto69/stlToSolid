"""The MCP server: tools, resources and prompts over the stlToSolid engine.

The server process holds no geometry: every engine call goes to a worker
subprocess (runner.py), so it starts in a blink and survives whatever a mesh
does to the kernel. stdout carries the protocol; nothing here prints to it,
and workers write to their own log file.
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, Optional

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import UserMessage
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import config as C
from . import files as F
from . import guide, runner

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# writes new files under the output folder and overwrites nothing
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
Units = Literal['mm', 'cm', 'm', 'in', 'ft']
Axis = Literal['x', 'y', 'z']
_JOB_ID = re.compile(r'^[0-9a-f]{12}$')
MAX_WAIT_S = 55.0          # a client's default request timeout is often 60 s
MAX_HANDOFF_BYTES = 8 * 1024 * 1024
MAX_PROMPT_IMAGE_BYTES = 3 * 1024 * 1024
MAX_PROMPT_IMAGE_SIDE = 2000
IMAGE_MEDIA = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}
CODE_TIMEOUT_S = float(os.environ.get('STLTOSOLID_CODE_TIMEOUT', 120))

Path_ = Annotated[str, Field(description='Absolute path of the mesh file')]
UnitsArg = Annotated[Units, Field(description="What the file's numbers mean. STL and OBJ carry no units.")]
ScaleArg = Annotated[float, Field(gt=0, le=1e6, description='Extra factor after units. Units only enlarge, so a cm design exported as mm needs 0.1.')]
TolArg = Annotated[float, Field(gt=0, le=5, description='Fit tolerance for lines, arcs and splines, mm')]
JoinArg = Annotated[float, Field(ge=0, le=20, description='Join loose section ends closer than this, mm (0 = never)')]
TrimArg = Annotated[float, Field(ge=0, le=5, description='Cut slivers thinner than this out of closed loops, mm (0 = off)')]
TidyArg = Annotated[bool, Field(description='Tidy corners: tangent fillets and exact corners (X-Ray scripts also add tangent constraints)')]
SharpenArg = Annotated[float, Field(ge=0, le=1, description='Draw rounds that stay within this of their corner as sharp corners, mm (0 = off)')]
OutDirArg = Annotated[Optional[str], Field(description='Folder for the results. Default: one folder per part under the output folder.')]


class JobResult(BaseModel):
    """A conversion or build: running, or finished with its files and report."""
    job_id: str
    status: str = Field(description='queued, running, done, error or cancelled')
    finished: bool = Field(description='False means it is still going: call job_status again')
    ok: bool = Field(description='True only when it finished and wrote its result')
    error: Optional[str] = None
    summary: str = Field(description='One sentence on where it stands')
    files: dict[str, str] = Field(default_factory=dict, description='Result files by kind, e.g. step, fusion_script')
    file_urls: dict[str, str] = Field(default_factory=dict, description='Download URLs, on a remote server')
    report: dict[str, Any] = Field(default_factory=dict, description='Route, deviations, face counts and surface types')
    log_tail: list[str] = Field(default_factory=list)
    elapsed_s: Optional[float] = None


def _summary(st):
    if not st['finished']:
        last = st['log_tail'][-1] if st['log_tail'] else ''
        return f"Still {st['status']}. Call job_status with this job_id. Last log line: {last}"
    if st['status'] == 'cancelled':
        return 'Cancelled.'
    if not st['ok']:
        return f"Failed: {st.get('error') or 'see the log'}"
    rep = st['report']
    stats = rep.get('output_stats') or {}
    bits = [rep.get('mode') or 'done']
    if stats.get('faces'):
        bits.append(f"{stats['faces']} faces")
    if stats.get('solids') is not None:
        bits.append(f"{stats['solids']} solid(s)")
    if stats.get('closed') is False:
        bits.append('NOT CLOSED: imports as surfaces')
    if rep.get('volume_mm3') is not None:
        bits.append(f"{rep['volume_mm3']:.1f} mm3")
    return 'Finished: ' + ', '.join(bits) + '.'


async def _follow(cfg, job_id, wait_s, ctx, started=None):
    """The job's state after it finishes or `wait_s` passes, whichever comes
    first, telling the client what the log says meanwhile."""
    t0 = time.monotonic()
    wait_s = max(0.0, min(float(wait_s), MAX_WAIT_S))
    last = None
    while True:
        st = await anyio.to_thread.run_sync(runner.job_state, cfg, job_id)
        if st is None:
            raise ToolError(f'There is no job {job_id}: finished jobs are cleared after a day.')
        waited = time.monotonic() - t0
        if st['finished'] or waited >= wait_s:
            break
        line = st['log_tail'][-1] if st['log_tail'] else st['status']
        if ctx is not None and line != last:
            await ctx.report_progress(waited, message=line[:200])
            last = line
        await anyio.sleep(min(1.0, wait_s - waited))
    st['summary'] = _summary(st)
    if started is not None:
        st['elapsed_s'] = round(time.time() - started, 1)
    return JobResult(**st)


def _images(renders):
    return [ImageContent(type='image', data=b64, mime_type='image/png') for b64 in renders]


def _with_images(data, images, text=None):
    """A tool result carrying typed data and pictures together."""
    body = text if text is not None else json.dumps(data, indent=1)
    return CallToolResult(content=[TextContent(type='text', text=body)] + images, structured_content=data)


def _curve_counts(sec):
    s = sec.get('stats') or {}
    return {k: s.get(k, 0) for k in ('loops', 'holes', 'open', 'lines', 'arcs', 'circles', 'splines')}


def _guard(fn):
    """Turn the service's refusals into tool errors the model reads."""
    import functools
    import inspect

    @functools.wraps(fn)
    async def wrapped(*a, **k):
        try:
            return await fn(*a, **k)
        except (F.PathRefused, runner.CallFailed) as e:
            raise ToolError(str(e)) from e
    wrapped.__signature__ = inspect.signature(fn)
    return wrapped


def build_server(cfg):
    """The configured server. Separate from main() so tests can drive it."""
    from stl_to_solid import __version__
    from stl_to_solid.service import jobs
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    jobs.DATA_DIR = cfg.jobs_dir
    os.environ[jobs.WORKER_WATCH_ENV] = str(os.getpid())
    jobs.cleanup_old()
    # a folder the caller named on a tool this session, so its files can be
    # handed to Fusion afterwards
    written = {cfg.out_root}

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            runner.stop_all()
            try:
                from stl_to_solid.service.preview import helper
                helper.close()
            except Exception:
                pass

    mcp = MCPServer('stltosolid', title='stlToSolid', version=__version__, instructions=guide.INSTRUCTIONS,
                    website_url='https://stltosolid.com', lifespan=lifespan)

    def out_dir_for(out_dir, stem):
        d = F.resolve_out_dir(cfg, out_dir, stem)
        written.add(d)
        return d

    # ------------------------------------------------------------------ mesh

    @mcp.tool(annotations=READ_ONLY, description=(
        'Look at a mesh file before converting it: size, triangle count, whether it is closed, its bodies '
        '(largest first), a warning when the units look wrong, and a suggested method. Takes a second or '
        'two on most parts.'))
    @_guard
    async def inspect_mesh(path: Path_, units: UnitsArg = 'mm', scale: ScaleArg = 1.0) -> dict[str, Any]:
        src = F.resolve_input(cfg, path, F.MESH_EXTS, 'mesh file')
        return await anyio.to_thread.run_sync(
            lambda: runner.call(cfg, 'inspect_mesh', src, {'units': units, 'scale': scale}))

    @mcp.tool(annotations=WRITES, description=(
        "Convert a mesh to a STEP solid. Method 'auto' tries prismatic (sketches and extrudes), then face "
        "groups, then faceted, held to the acceptance limits. Method 'loft' slices along an axis and lofts "
        'the outlines into one smooth solid. Runs one job at a time and can take from seconds to over an '
        'hour: it answers after wait_s with a job_id if it is still going. Then call job_status.'))
    @_guard
    async def convert_mesh(
        path: Path_,
        method: Annotated[Literal['auto', 'loft'], Field(description='auto for designed parts, loft for smooth ones')] = 'auto',
        bodies: Annotated[Optional[list[int]], Field(description='Body indices from inspect_mesh; default all')] = None,
        units: UnitsArg = 'mm',
        scale: ScaleArg = 1.0,
        tol: TolArg = 0.08,
        accept_p95: Annotated[float, Field(gt=0, le=10, description='Max p95 surface deviation, mm')] = 0.25,
        accept_max: Annotated[float, Field(gt=0, le=10, description='Max single-point deviation, mm')] = 0.26,
        accept_hole_max: Annotated[float, Field(gt=0, le=10, description='Max deviation on bores, mm')] = 0.10,
        accept_vol_pct: Annotated[float, Field(gt=0, le=50, description='Max volume error, percent')] = 2.0,
        force_prismatic: Annotated[bool, Field(description='Attempt a prismatic fit even for scan-like input')] = False,
        face_groups: Annotated[bool, Field(description='Try the face-group route between prismatic and faceted')] = True,
        reduce_tol: Annotated[float, Field(ge=0, le=5, description='Faceted output: decimate curved regions within this, mm (0 = off)')] = 0.05,
        slice_mm: Annotated[float, Field(gt=0, le=50, description='Loft: section spacing, mm')] = 0.2,
        slice_axis: Annotated[Literal['auto', 'x', 'y', 'z'], Field(description='Loft: slicing axis')] = 'auto',
        loft_ruled: Annotated[bool, Field(description='Loft: one face per section pair instead of smooth')] = False,
        slice_join: Annotated[float, Field(ge=0, le=20, description='Loft: join loose section ends closer than this, mm')] = 2.5,
        slice_trim: Annotated[float, Field(ge=0, le=5, description='Loft: cut slivers thinner than this, mm')] = 0.0,
        slice_from: Annotated[Optional[float], Field(description='Partial loft: start plane, mm from the bounding-box centre')] = None,
        slice_range_mm: Annotated[float, Field(ge=0, le=2000, description='Partial loft: length from the start plane, mm (0 = whole body)')] = 0.0,
        slice_range_dir: Annotated[Literal['+', '-'], Field(description='Partial loft: direction from the start plane')] = '-',
        out_dir: OutDirArg = None,
        wait_s: Annotated[Optional[float], Field(ge=0, le=MAX_WAIT_S, description='Seconds to wait before answering with a job_id')] = None,
        ctx: Context = None,
    ) -> JobResult:
        src = F.resolve_input(cfg, path, F.MESH_EXTS, 'mesh file')
        stem = F.safe_stem(src)
        d = out_dir_for(out_dir, stem)
        params = dict(tol=tol, accept_p95=accept_p95, accept_max=accept_max, accept_hole_max=accept_hole_max,
                      accept_vol_pct=accept_vol_pct, force_prismatic=force_prismatic, face_groups=face_groups,
                      reduce_tol=reduce_tol, units=units, scale=scale, bodies=bodies, method=method,
                      slice_mm=slice_mm, slice_axis=slice_axis, loft_ruled=loft_ruled, slice_join=slice_join,
                      slice_trim=slice_trim, slice_from=slice_from, slice_range_mm=slice_range_mm,
                      slice_range_dir=slice_range_dir)
        started = time.time()
        job_id = await anyio.to_thread.run_sync(lambda: runner.start_job(cfg, params, stem, d, src))
        return await _follow(cfg, job_id, cfg.default_wait_s if wait_s is None else wait_s, ctx, started)

    @mcp.tool(annotations=READ_ONLY, description=(
        'Where a conversion or build stands. Waits up to wait_s for it to finish, so call it again straight '
        'away while `finished` is false.'))
    @_guard
    async def job_status(
        job_id: Annotated[str, Field(description='The job_id a tool answered with')],
        wait_s: Annotated[float, Field(ge=0, le=MAX_WAIT_S, description='Seconds to wait for it to finish')] = 30.0,
        ctx: Context = None,
    ) -> JobResult:
        if not _JOB_ID.match(job_id):
            raise ToolError(f'{job_id!r} is not a job id.')
        return await _follow(cfg, job_id, wait_s, ctx)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
              description='Stop a running conversion or build. Its partial work is discarded; finished results stay.')
    @_guard
    async def cancel_job(job_id: Annotated[str, Field(description='The job_id to stop')]) -> dict[str, Any]:
        if not _JOB_ID.match(job_id):
            raise ToolError(f'{job_id!r} is not a job id.')
        what = await anyio.to_thread.run_sync(jobs.cancel, job_id)
        if what == 'unknown':
            raise ToolError(f'There is no job {job_id}.')
        return {'job_id': job_id, 'status': what}

    @mcp.tool(annotations=READ_ONLY, description=(
        'Which axis a whole-body loft would slice along, and the score of each axis (fewest one-slice runs '
        'wins). Under a few seconds.'))
    @_guard
    async def suggest_loft_axis(
        path: Path_, units: UnitsArg = 'mm', scale: ScaleArg = 1.0,
        slice_mm: Annotated[float, Field(gt=0, le=50, description='Section spacing, mm')] = 0.2,
        join: JoinArg = 2.5, trim: TrimArg = 0.0,
    ) -> dict[str, Any]:
        src = F.resolve_input(cfg, path, F.MESH_EXTS, 'mesh file')
        return await anyio.to_thread.run_sync(lambda: runner.call(
            cfg, 'loft_axis', src, dict(units=units, scale=scale, slice_mm=slice_mm, join=join, trim=trim)))

    @mcp.tool(annotations=READ_ONLY, description=(
        'Slice a mesh at one plane and see it: a picture of the fitted outline with a millimetre grid, and '
        'how many lines, arcs, circles and splines it took. offset_mm is measured from the bounding-box '
        'centre along the axis. Red curves in the picture are open chains, where the mesh leaks.'))
    @_guard
    async def trace_section(
        path: Path_, axis: Annotated[Axis, Field(description='The axis the plane is square to')] = 'z',
        offset_mm: Annotated[float, Field(description='Plane position, mm from the bounding-box centre')] = 0.0,
        tol: TolArg = 0.08, join: JoinArg = 2.5, trim: TrimArg = 0.0,
        outline: Annotated[bool, Field(description='Closed outer outlines only: leave out open chains and holes')] = False,
        units: UnitsArg = 'mm', scale: ScaleArg = 1.0,
        include_curves: Annotated[bool, Field(description='Also return every fitted curve as 3-D data. Can be long.')] = False,
        tidy: TidyArg = False, sharpen: SharpenArg = 0.0,
    ) -> CallToolResult:
        src = F.resolve_input(cfg, path, F.MESH_EXTS, 'mesh file')
        sec = await anyio.to_thread.run_sync(lambda: runner.call(cfg, 'trace_section', src, dict(
            axis=axis, offset=offset_mm, tol=tol, join=join, trim=trim, outline=outline, units=units, scale=scale,
            tidy=tidy, sharpen=sharpen)))
        png = sec.pop('png_b64')
        loops, opens = sec.pop('loops', []), sec.pop('open', [])
        data = {'axis': axis, 'at_mm': sec.get('at'), 'offset_mm': sec.get('offset'), 'extent_mm': sec.get('extent'),
                'empty': bool(sec.get('empty')), 'curves': _curve_counts(sec), 'areas_mm2': sec.get('areas_mm2', []),
                'stats': sec.get('stats', {})}
        if include_curves:
            data.update(loops=loops, open_chains=opens)
        return _with_images(data, _images([png]))

    @mcp.tool(annotations=READ_ONLY, description=(
        'Where a stack of X-Ray section planes would go: their offsets and how many there are. Cheap: no '
        'tracing. from_mm and to_mm are measured from the bounding-box centre along the axis.'))
    @_guard
    async def plan_xray(
        path: Path_, axis: Annotated[Axis, Field(description='The axis the planes are square to')],
        from_mm: Annotated[float, Field(description='Start plane, mm from the centre')],
        to_mm: Annotated[float, Field(description='End plane, mm from the centre')],
        step_mm: Annotated[float, Field(gt=0, le=50, description='Spacing, mm')] = 0.2,
        units: UnitsArg = 'mm', scale: ScaleArg = 1.0,
    ) -> dict[str, Any]:
        src = F.resolve_input(cfg, path, F.MESH_EXTS, 'mesh file')
        return await anyio.to_thread.run_sync(lambda: runner.call(cfg, 'plan_xray', src, {
            'axis': axis, 'from': from_mm, 'to': to_mm, 'step': step_mm, 'units': units, 'scale': scale}))

    @mcp.tool(annotations=WRITES, description=(
        'Write a Fusion 360 script with one real sketch per slice, between two planes. With extrude, each '
        'slice is also extruded to the next, so the stack comes out as solid slabs. No STEP. Up to 500 '
        'slices and about four minutes of fitting: call plan_xray first.'))
    @_guard
    async def xray_fusion_script(
        path: Path_, axis: Annotated[Axis, Field(description='The axis the planes are square to')],
        from_mm: Annotated[float, Field(description='Start plane, mm from the centre')],
        to_mm: Annotated[float, Field(description='End plane, mm from the centre')],
        step_mm: Annotated[float, Field(gt=0, le=50, description='Spacing, mm')] = 0.2,
        tol: TolArg = 0.08, join: JoinArg = 2.5, trim: TrimArg = 0.0,
        outline: Annotated[bool, Field(description='Closed outer outlines only')] = False,
        extrude: Annotated[bool, Field(description='Extrude each slice to the next: solid slabs')] = False,
        units: UnitsArg = 'mm', scale: ScaleArg = 1.0, out_dir: OutDirArg = None,
        tidy: TidyArg = False, sharpen: SharpenArg = 0.0,
    ) -> dict[str, Any]:
        src = F.resolve_input(cfg, path, F.MESH_EXTS, 'mesh file')
        stem = F.safe_stem(src)
        d = out_dir_for(out_dir, stem)
        tmp = F.fresh_path(d, f'.{stem}_xray.tmp')

        def go():
            try:
                res = runner.call(cfg, 'xray_script', src, {
                    'axis': axis, 'from': from_mm, 'to': to_mm, 'step': step_mm, 'tol': tol, 'join': join,
                    'trim': trim, 'outline': outline, 'extrude': extrude, 'units': units, 'scale': scale,
                    'budget_s': cfg.xray_budget_s, 'tidy': tidy, 'sharpen': sharpen},
                    out_path=tmp, timeout=cfg.xray_budget_s + 120)
                final = F.fresh_path(d, stem + res.pop('name'))
                os.replace(tmp, final)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            res['script'] = F.public(cfg, final)
            url = F.url_for(cfg, final)
            if url:
                res['script_url'] = url
            return res

        return await anyio.to_thread.run_sync(go)

    @mcp.tool(annotations=READ_ONLY, description=(
        'Read a STEP file back the way a CAD program will: solids, faces, surface types, bounding box, '
        'volume, and whether it is closed and valid. Use it to check a result.'))
    @_guard
    async def inspect_step(path: Annotated[str, Field(description='Absolute path of the .step file')]) -> dict[str, Any]:
        src = F.resolve_input(cfg, path, ('.step', '.stp'), 'STEP file')
        return await anyio.to_thread.run_sync(lambda: runner.call(cfg, 'inspect_step', src))

    # ------------------------------------------------------------- blueprint

    @mcp.tool(annotations=READ_ONLY, description=(
        'Check a Blueprint recipe and draw it: four pictures (front, top, right, isometric), its size and '
        'volume, or the list of checks that failed. No files are written. Use it while reading a drawing.'))
    @_guard
    async def preview_recipe(
        recipe: Annotated[dict[str, Any], Field(description='The recipe; see stltosolid://blueprint/recipe-schema')],
    ) -> CallToolResult:
        def go():
            import tempfile
            from stl_to_solid.service.preview import PreviewError, helper
            with tempfile.TemporaryDirectory(dir=cfg.jobs_dir, prefix='preview-') as d:
                try:
                    return helper.build(recipe, d, render=True)
                except PreviewError as e:
                    if e.validation:
                        return {'ok': False, 'errors': e.validation.get('errors', []),
                                'warnings': e.validation.get('warnings', [])}
                    raise runner.CallFailed(str(e)) from e
        ans = await anyio.to_thread.run_sync(go)
        if not ans.get('ok'):
            return _with_images({'buildable': False, 'errors': ans['errors'], 'warnings': ans['warnings']}, [])
        renders = ans.get('renders') or []
        data = {'buildable': True, 'errors': [], 'warnings': ans.get('warnings', []), 'bbox': ans.get('bbox'),
                'volume_mm3': ans.get('volume_mm3'), 'solids': ans.get('solids'),
                'overall_check': ans.get('overall_check'),
                'features': [{k: f.get(k) for k in ('id', 'name', 'op')} for f in ans.get('features', [])],
                'views': [r['caption'] for r in renders]}
        return _with_images(data, _images([r['png_b64'] for r in renders]))

    @mcp.tool(annotations=WRITES, description=(
        'Build a Blueprint recipe into a STEP and a parametric Fusion 360 script (User Parameters, sketches, '
        'extrudes). The recipe is checked first and refused with the reasons if it cannot be built.'))
    @_guard
    async def build_recipe(
        recipe: Annotated[dict[str, Any], Field(description='The recipe; see stltosolid://blueprint/recipe-schema')],
        title: Annotated[Optional[str], Field(description='Name for the part and its files')] = None,
        out_dir: OutDirArg = None,
        wait_s: Annotated[Optional[float], Field(ge=0, le=MAX_WAIT_S, description='Seconds to wait before answering with a job_id')] = None,
        ctx: Context = None,
    ) -> JobResult:
        from stl_to_solid.blueprint import normalize, validate
        r = await anyio.to_thread.run_sync(lambda: normalize(recipe))
        rep = await anyio.to_thread.run_sync(lambda: validate(r))
        if rep.errors:
            raise ToolError(f'The recipe is not valid ({len(rep.errors)} problem(s)): ' + '; '.join(rep.errors[:8]))
        stem = F.safe_stem(title or r.get('name') or 'part')
        d = out_dir_for(out_dir, stem)
        params = {'tool': 'blueprint', 'recipe': r, 'title': title or stem}
        started = time.time()
        job_id = await anyio.to_thread.run_sync(lambda: runner.start_job(cfg, params, stem, d))
        return await _follow(cfg, job_id, cfg.default_wait_s if wait_s is None else wait_s, ctx, started)

    if cfg.allow_code:
        @mcp.tool(annotations=WRITES, description=(
            'Run a short CadQuery script and write the part it builds as a STEP, for shapes a recipe cannot '
            'draw: bends, blends, tilted bosses, fillets. The script may import only cadquery and math, has '
            'no file access, and must assign the part to `result`. Rules at stltosolid://blueprint/code-rules. '
            'It runs in its own process with a time limit.'))
        @_guard
        async def build_cadquery(
            code: Annotated[str, Field(max_length=200000, description='The CadQuery script')],
            units: Annotated[Literal['mm', 'in'], Field(description="The script's units; inches are scaled for you")] = 'mm',
            title: Annotated[Optional[str], Field(description='Name for the part and its files')] = None,
            out_dir: OutDirArg = None,
            wait_s: Annotated[Optional[float], Field(ge=0, le=MAX_WAIT_S, description='Seconds to wait before answering with a job_id')] = None,
            ctx: Context = None,
        ) -> CallToolResult:
            from stl_to_solid.blueprint.freeform import check_code
            errors = check_code(code)
            if errors:
                raise ToolError(f'The script was refused ({len(errors)} problem(s)): ' + '; '.join(errors[:8]))
            stem = F.safe_stem(title or 'part')
            d = out_dir_for(out_dir, stem)
            params = {'tool': 'blueprint', 'code': code, 'units': units, 'title': title or stem,
                      'timeout_s': CODE_TIMEOUT_S}
            started = time.time()
            job_id = await anyio.to_thread.run_sync(lambda: runner.start_job(cfg, params, stem, d))
            res = await _follow(cfg, job_id, cfg.default_wait_s if wait_s is None else wait_s, ctx, started)
            pics = []
            for kind in ('render_front', 'render_top', 'render_right', 'render_iso'):
                p = res.files.get(kind)
                if p:
                    full = os.path.join(cfg.root, p) if cfg.http and cfg.root else p
                    with open(full, 'rb') as f:
                        pics.append(base64.b64encode(f.read()).decode('ascii'))
            return _with_images(res.model_dump(), _images(pics))

    # ---------------------------------------------------------- Fusion hand-off

    @mcp.tool(annotations=READ_ONLY, description=(
        "The Python to run in Fusion 360 for a result, to pass to Autodesk's Fusion MCP script tool: the "
        'script itself for a .py, or a small importer for a .step. Only for files this server wrote.'))
    @_guard
    async def fusion_handoff(path: Annotated[str, Field(description='A .py or .step result from this server')]) -> dict[str, Any]:
        src = F.resolve_input(cfg, path, ('.py', '.step', '.stp'), 'result file')
        if not any(F._inside(src, os.path.realpath(d)) for d in written):
            raise F.PathRefused(f'{path} is not in an output folder of this server, so it is not handed over.')
        if src.lower().endswith('.py'):
            if os.path.getsize(src) > MAX_HANDOFF_BYTES:
                raise F.PathRefused(f'{os.path.basename(src)} is too long to pass through a tool call. '
                                    'Run it by hand from Scripts and Add-Ins.')
            with open(src, encoding='utf-8') as f:
                return {'kind': 'script', 'python': f.read(), 'path': F.public(cfg, src)}
        if cfg.http:
            raise F.PathRefused('This server is remote, so Fusion cannot open its files by path. Download the '
                                'STEP from its URL first.')
        return {'kind': 'step_importer', 'path': src, 'python': _step_importer(src)}

    # ------------------------------------------------------------ remote files

    if cfg.http:
        @mcp.tool(annotations=READ_ONLY, description=(
            'List the files in the shared folder of this remote server, with their paths for the other '
            'tools and their download URLs. Upload with PUT /files/<name>, or copy into the shared folder.'))
        @_guard
        async def list_files() -> dict[str, Any]:
            out = []
            for base, dirs, names in os.walk(cfg.root):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for n in names:
                    if not n.startswith('.') and len(out) < 500:
                        p = os.path.join(base, n)
                        out.append({'path': F.public(cfg, p), 'bytes': os.path.getsize(p), 'url': F.url_for(cfg, p)})
            return {'root': cfg.root, 'files': sorted(out, key=lambda x: x['path'])}

    # --------------------------------------------------------------- resources

    @mcp.resource('stltosolid://guide/which-tool', name='Which tool to use', mime_type='text/markdown',
                  description='How to choose a tool, the settings and their defaults, and how to read a report.')
    def which_tool() -> str:
        return guide.WHICH_TOOL

    @mcp.resource('stltosolid://blueprint/recipe-schema', name='Blueprint recipe schema', mime_type='application/json',
                  description='The JSON schema of the recipe that preview_recipe and build_recipe take.')
    def recipe_schema() -> str:
        from stl_to_solid.blueprint import SCHEMA
        return json.dumps(SCHEMA, indent=1)

    @mcp.resource('stltosolid://blueprint/reading-rules', name='How to read a drawing', mime_type='text/markdown',
                  description='The rules for reading a dimensioned drawing into a recipe.')
    def reading_rules() -> str:
        from stl_to_solid.blueprint.prompts import SYSTEM_PROMPT
        return SYSTEM_PROMPT

    if cfg.allow_code:
        @mcp.resource('stltosolid://blueprint/code-rules', name='How to write a CadQuery part', mime_type='text/markdown',
                      description='The rules build_cadquery holds a script to.')
        def code_rules() -> str:
            from stl_to_solid.blueprint.prompts import CODE_SYSTEM_PROMPT
            return CODE_SYSTEM_PROMPT

    @mcp.resource('stltosolid://jobs/{job_id}/log', name='Job log', mime_type='text/plain',
                  description="A conversion's full log so far.")
    def job_log(job_id: str) -> str:
        if not _JOB_ID.match(job_id):
            raise ValueError(f'{job_id!r} is not a job id.')
        st = jobs.public_state(job_id)
        if st is None:
            raise ValueError(f'There is no job {job_id}.')
        return st.get('log') or ''

    @mcp.resource('stltosolid://jobs/{job_id}/report', name='Job report', mime_type='application/json',
                  description="A finished conversion's fidelity report and files.")
    def job_report(job_id: str) -> str:
        if not _JOB_ID.match(job_id):
            raise ValueError(f'{job_id!r} is not a job id.')
        st = runner.job_state(cfg, job_id)
        if st is None:
            raise ValueError(f'There is no job {job_id}.')
        return json.dumps(st, indent=1)

    # ----------------------------------------------------------------- prompts

    @mcp.prompt(name='convert-for-fusion', title='Convert a mesh for Fusion 360',
                description='Inspect a mesh, choose a tool, convert it, check the result and say how to use it.')
    def convert_for_fusion(path: str) -> list[UserMessage]:
        return [UserMessage(guide.convert_for_fusion(path))]

    @mcp.prompt(name='read-drawing', title='Build a part from a drawing',
                description='Read a dimensioned drawing into a recipe and build a STEP and a parametric Fusion script.')
    def read_drawing(drawing_path: str) -> list[UserMessage]:
        from stl_to_solid.blueprint.prompts import SYSTEM_PROMPT
        src = F.resolve_input(cfg, drawing_path, F.DRAWING_EXTS, 'drawing image')
        data, mime = _prompt_image(src)
        tail = guide.READ_DRAWING_TAIL + (guide.READ_DRAWING_CODE_ON if cfg.allow_code else guide.READ_DRAWING_CODE_OFF)
        return [UserMessage(SYSTEM_PROMPT + tail),
                UserMessage(ImageContent(type='image', data=data, mime_type=mime)),
                UserMessage('Build this part from the drawing above.')]

    @mcp.prompt(name='xray-to-fusion', title='Cross-section sketches for Fusion 360',
                description='Trace a stack of sections through a mesh into a Fusion script of real sketches.')
    def xray_to_fusion(path: str, axis: str = '') -> list[UserMessage]:
        return [UserMessage(guide.xray_to_fusion(path, axis))]

    @mcp.prompt(name='send-to-fusion', title='Send a result into Fusion 360',
                description="Run a result in the open Fusion design through Autodesk's own Fusion MCP server.")
    def send_to_fusion(path: str) -> list[UserMessage]:
        return [UserMessage(guide.send_to_fusion(path))]

    return mcp


def _step_importer(path):
    return f"""import adsk.core, adsk.fusion


def run(_context):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if not design:
        raise RuntimeError('Open a design first: the active document is not a Fusion design.')
    options = app.importManager.createSTEPImportOptions({path!r})
    app.importManager.importToTarget(options, design.rootComponent)
    print('Imported ' + {os.path.basename(path)!r})
"""


def _prompt_image(path):
    """(base64, mime) of a drawing for a prompt message: as it is when small,
    else shrunk, because clients refuse a very large image in a message."""
    ext = os.path.splitext(path)[1].lstrip('.').lower()
    size = os.path.getsize(path)
    if size <= MAX_PROMPT_IMAGE_BYTES:
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode('ascii'), IMAGE_MEDIA[ext]
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert('RGB')
        im.thumbnail((MAX_PROMPT_IMAGE_SIDE, MAX_PROMPT_IMAGE_SIDE))
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=88)
    return base64.b64encode(out.getvalue()).decode('ascii'), 'image/jpeg'


def _check():
    """Prove the install works, for a person at a terminal: `--check`."""
    import tempfile
    from stl_to_solid import __version__
    print(f'stlToSolid {__version__}, Python {sys.version.split()[0]}, {sys.platform}')
    t0 = time.time()
    with tempfile.TemporaryDirectory() as d:
        cfg = C.from_args(out=d)
        from stl_to_solid.service import jobs
        os.makedirs(cfg.jobs_dir, exist_ok=True)
        jobs.DATA_DIR = cfg.jobs_dir
        stl = os.path.join(d, 'box.stl')
        tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        v = [(0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0), (0, 0, 5), (20, 0, 5), (20, 10, 5), (0, 10, 5)]
        with open(stl, 'w') as f:
            f.write('solid box\n')
            for a, b, c in tris:
                f.write(' facet normal 0 0 0\n  outer loop\n')
                for i in (a, b, c):
                    f.write('   vertex %g %g %g\n' % v[i])
                f.write('  endloop\n endfacet\n')
            f.write('endsolid box\n')
        info = runner.call(cfg, 'inspect_mesh', stl)
        print(f"mesh stack OK: {info['triangles']} triangles, {info['bbox_mm']} mm, in {time.time() - t0:.1f} s")
        t0 = time.time()
        out = runner.call(cfg, 'trace_section', stl, {'axis': 'z'})
        print(f"section tracing OK: {out['stats']['loops']} loop(s), in {time.time() - t0:.1f} s")
        t0 = time.time()
        params = dict(tol=0.08, accept_p95=0.25, accept_max=0.26, accept_hole_max=0.1, accept_vol_pct=2.0,
                      force_prismatic=False, face_groups=True, reduce_tol=0.05, units='mm', scale=1.0, method='auto')
        job = runner.start_job(cfg, params, 'box', d, stl)
        while not (st := runner.job_state(cfg, job))['finished']:
            time.sleep(0.5)
        if not st['ok']:
            print('conversion FAILED:', st['error'])
            sys.exit(1)
        print(f"geometry kernel OK: {st['report'].get('mode')}, {st['report']['output_stats']['faces']} faces, "
              f'in {time.time() - t0:.1f} s')
    print('All good. Your MCP client can start this server now.')


def main(argv=None):
    from stl_to_solid import __version__
    ap = argparse.ArgumentParser(prog='stltosolid-mcp', description='stlToSolid as an MCP server (stdio by default).')
    ap.add_argument('--version', action='version', version=__version__)
    ap.add_argument('--check', action='store_true', help='test the install end to end and exit')
    ap.add_argument('--out', help=f'folder for results (env STLTOSOLID_OUT; default {C.DEFAULT_OUT})')
    ap.add_argument('--root', help='only read input files inside this folder (env STLTOSOLID_MCP_ROOT)')
    ap.add_argument('--allow-code', action='store_true',
                    help='enable build_cadquery, which runs model-written CadQuery under an allow-list (env STLTOSOLID_MCP_ALLOW_CODE)')
    ap.add_argument('--http', action='store_true', help='serve Streamable HTTP for a remote client instead of stdio')
    ap.add_argument('--host', default='127.0.0.1', help='HTTP: address to listen on')
    ap.add_argument('--port', type=int, default=8322, help='HTTP: port')
    ap.add_argument('--token', help='HTTP: bearer token clients must send (env STLTOSOLID_MCP_TOKEN)')
    ap.add_argument('--no-auth', action='store_true', help='HTTP: allow no token on a non-loopback address')
    ap.add_argument('--base-url', help='HTTP: the URL clients reach this server at, for download links (env STLTOSOLID_MCP_URL)')
    args = ap.parse_args(argv)
    if args.check:
        _check()
        return
    if args.http:
        from .http import serve
        serve(args)
        return
    cfg = C.from_args(out=args.out, root=args.root, allow_code=args.allow_code)
    print(f'stltosolid-mcp {__version__}: results go to {cfg.out_root}', file=sys.stderr)
    build_server(cfg).run('stdio')


if __name__ == '__main__':
    main()
