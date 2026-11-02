"""The MCP server, driven the way a client drives it: a real stdio subprocess
for the end-to-end path, and an in-process client for the rest. Parts come from
tests/synth.py, so the test data needs no samples."""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip('mcp')
pytest.importorskip('PIL')

from mcp import Client                                             # noqa: E402
from mcp.client.stdio import StdioServerParameters                 # noqa: E402

from stl_to_solid_mcp import config as C, files as F, runner       # noqa: E402
from stl_to_solid_mcp.server import build_server                   # noqa: E402

from . import synth                                                # noqa: E402

BOX = {'name': 'box', 'units': 'mm', 'overall': {'w': 20, 'd': 15, 'h': 10},
       'params': [{'name': 'w', 'value': 20, 'source': 'label', 'inferred': False},
                  {'name': 'h', 'value': 10, 'source': 'label', 'inferred': False}],
       'features': [{'id': 'body', 'op': 'new_body', 'plane': 'XY', 'offset': 0, 'direction': '+',
                     'distance': 'h', 'through_all': False,
                     'shapes': [{'kind': 'rect', 'center': {'u': 'w/2', 'v': 7.5}, 'w': 'w', 'h': 15,
                                 'corner_radius': None}],
                     'source': {'view': 'top', 'labels': ['20', '15', '10'], 'inferred': False},
                     'confidence': 0.9}],
       'views_found': ['top', 'front'], 'notes': []}


@pytest.fixture
def part(tmp_path):
    path = str(tmp_path / 'plate.stl')
    synth.export(synth.plate_holes(), path)
    return path


@pytest.fixture
def cfg(tmp_path):
    return C.from_args(out=str(tmp_path / 'out'), allow_code=True)


def drive(cfg, fn):
    async def go():
        async with Client(build_server(cfg)) as c:
            return await fn(c)
    return asyncio.run(go())


def test_tool_surface(cfg, tmp_path):
    names = drive(cfg, lambda c: c.list_tools())
    got = {t.name: t for t in names.tools}
    assert set(got) == {'inspect_mesh', 'convert_mesh', 'job_status', 'cancel_job', 'suggest_loft_axis',
                        'trace_section', 'plan_xray', 'xray_fusion_script', 'inspect_step', 'preview_recipe',
                        'build_recipe', 'build_cadquery', 'fusion_handoff'}
    assert got['inspect_mesh'].annotations.read_only_hint is True
    assert got['convert_mesh'].annotations.destructive_hint is False
    # the one tool that runs model-written code is opt-in
    off = C.from_args(out=str(tmp_path / 'off'))
    assert 'build_cadquery' not in {t.name for t in drive(off, lambda c: c.list_tools()).tools}


def test_convert_defaults_match_the_web_app(cfg):
    pytest.importorskip('fastapi')
    from backend.main import ConvertParams
    tools = drive(cfg, lambda c: c.list_tools()).tools
    props = next(t for t in tools if t.name == 'convert_mesh').input_schema['properties']
    web = ConvertParams.model_fields
    same = [k for k in props if k in web]
    assert len(same) >= 18
    for k in same:
        assert props[k].get('default') == web[k].default, k


def test_inspect_then_convert_end_to_end(cfg, part):
    async def go(c):
        info = (await c.call_tool('inspect_mesh', {'path': part})).structured_content
        res = (await c.call_tool('convert_mesh', {'path': part, 'wait_s': 55})).structured_content
        while not res['finished']:
            res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
        step = (await c.call_tool('inspect_step', {'path': res['files']['step']})).structured_content
        return info, res, step
    info, res, step = drive(cfg, go)
    assert info['watertight'] and info['body_count'] == 1 and info['suggested_bodies'] == [0]
    assert info['suggested_method'] == 'auto'
    assert res['ok'] and res['report']['mode'] == 'prismatic'
    assert 'prismatic' in res['summary'] and res['log_tail'] == []
    # delivered next to the part under its own name, not left in the scratch folder
    assert os.path.dirname(res['files']['step']) == os.path.join(cfg.out_root, 'plate')
    assert os.path.basename(res['files']['step']) == 'plate.step'
    assert os.path.exists(res['files']['fusion_script']) and os.path.exists(res['files']['cadquery_script'])
    assert 'params' not in res['report']
    assert step['solids'] == 1 and step['closed'] is True and step['valid'] is True
    assert step['volume_mm3'] > 0 and len(step['bbox_mm']) == 3


def test_same_result_as_the_command_line(cfg, part, tmp_path):
    """The server is a door into the engine, so a conversion must be the one
    the CLI makes: the same bytes, minus the timestamp a STEP header carries."""
    cli = str(tmp_path / 'cli.step')
    subprocess.run([sys.executable, '-m', 'stl_to_solid.pipeline', part, cli, '--quiet'], check=True,
                   capture_output=True)

    async def go(c):
        res = (await c.call_tool('convert_mesh', {'path': part, 'wait_s': 55})).structured_content
        while not res['finished']:
            res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
        return res
    res = drive(cfg, go)

    def body(path):
        with open(path, errors='replace') as f:
            return [ln for ln in f if not ln.startswith('FILE_NAME') and not ln.startswith('FILE_DESCRIPTION')]
    assert body(res['files']['step']) == body(cli)


def test_results_never_overwrite(cfg, part):
    async def go(c):
        out = []
        for _ in range(2):
            res = (await c.call_tool('convert_mesh', {'path': part, 'wait_s': 55})).structured_content
            while not res['finished']:
                res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
            out.append(res['files']['step'])
        # asking again must not copy the files a second time
        again = (await c.call_tool('job_status', {'job_id': res['job_id'], 'wait_s': 0})).structured_content
        out.append(again['files']['step'])
        return out
    a, b, c = drive(cfg, go)
    assert os.path.basename(a) == 'plate.step' and os.path.basename(b) == 'plate-2.step' and c == b
    assert sorted(n for n in os.listdir(os.path.dirname(a)) if n.endswith('.step')) == ['plate-2.step', 'plate.step']


def test_a_long_job_hands_back_its_id_and_can_be_cancelled(cfg, part):
    async def go(c):
        res = (await c.call_tool('convert_mesh', {'path': part, 'wait_s': 0})).structured_content
        assert res['finished'] is False and 'job_status' in res['summary']
        stop = (await c.call_tool('cancel_job', {'job_id': res['job_id']})).structured_content
        for _ in range(40):
            res = (await c.call_tool('job_status', {'job_id': res['job_id'], 'wait_s': 1})).structured_content
            if res['finished']:
                break
        return stop, res
    stop, res = drive(cfg, go)
    assert stop['status'] in ('cancelled', 'not_running')
    assert res['finished'] and (res['status'] == 'cancelled' or res['ok'])


def test_trace_section_answers_with_a_picture(cfg, part):
    r = drive(cfg, lambda c: c.call_tool('trace_section', {'path': part, 'axis': 'z'}))
    kinds = [b.type for b in r.content]
    assert kinds == ['text', 'image'] and r.content[1].mime_type == 'image/png'
    assert base64.b64decode(r.content[1].data)[:4] == b'\x89PNG'
    d = r.structured_content
    assert d['curves']['loops'] >= 1 and d['curves']['circles'] >= 1 and 'loops' not in d
    full = drive(cfg, lambda c: c.call_tool('trace_section', {'path': part, 'include_curves': True}))
    assert full.structured_content['loops']


def test_xray_plan_and_script(cfg, part):
    async def go(c):
        plan = (await c.call_tool('plan_xray', {'path': part, 'axis': 'z', 'from_mm': -1, 'to_mm': 1,
                                                'step_mm': 1})).structured_content
        one = (await c.call_tool('xray_fusion_script', {'path': part, 'axis': 'z', 'from_mm': -1, 'to_mm': 1,
                                                        'step_mm': 1, 'extrude': True})).structured_content
        over = await c.call_tool('xray_fusion_script', {'path': part, 'axis': 'x', 'from_mm': -100, 'to_mm': 100,
                                                        'step_mm': 0.01})
        return plan, one, over
    plan, one, over = drive(cfg, go)
    assert plan['count'] == 3 and plan['over_cap'] is False
    assert one['slices'] == 3 and one['script'].endswith('_solid.py') and os.path.exists(one['script'])
    with open(one['script']) as f:
        assert 'def run(' in f.read()
    assert not [n for n in os.listdir(os.path.dirname(one['script'])) if n.endswith('.tmp')]
    assert over.is_error and 'over the limit' in over.content[0].text


def test_recipe_preview_and_build(cfg):
    async def go(c):
        ok = await c.call_tool('preview_recipe', {'recipe': BOX})
        bad = await c.call_tool('preview_recipe', {'recipe': dict(BOX, features=[])})
        res = (await c.call_tool('build_recipe', {'recipe': BOX, 'title': 'my box', 'wait_s': 55})).structured_content
        while not res['finished']:
            res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
        refused = await c.call_tool('build_recipe', {'recipe': dict(BOX, features=[])})
        return ok, bad, res, refused
    ok, bad, res, refused = drive(cfg, go)
    assert [b.type for b in ok.content] == ['text'] + ['image'] * 4
    assert ok.structured_content['buildable'] is True
    assert ok.structured_content['bbox']['size'] == pytest.approx([20, 15, 10])
    assert bad.structured_content['buildable'] is False and bad.structured_content['errors']
    assert res['ok'] and os.path.basename(res['files']['step']) == 'my_box.step'
    assert os.path.exists(res['files']['fusion_script']) and os.path.exists(res['files']['recipe'])
    assert refused.is_error and 'not valid' in refused.content[0].text


def test_code_tool_runs_allowed_and_refuses_the_rest(cfg):
    async def go(c):
        good = await c.call_tool('build_cadquery', {'code': "import cadquery as cq\nresult = cq.Workplane('XY').box(4, 5, 6)\n",
                                                    'wait_s': 55})
        bad = await c.call_tool('build_cadquery', {'code': "import os\nresult = os.system('true')\n"})
        return good, bad
    good, bad = drive(cfg, go)
    d = good.structured_content
    assert d['ok'] and os.path.exists(d['files']['step']) and 'fusion_script' not in d['files']
    assert sum(b.type == 'image' for b in good.content) == 4
    assert bad.is_error and 'only `import cadquery' in bad.content[0].text


def test_paths_are_refused_with_a_sentence(cfg, tmp_path):
    note = tmp_path / 'secret.txt'
    note.write_text('x')

    async def go(c):
        return [await c.call_tool('inspect_mesh', {'path': str(note)}),
                await c.call_tool('inspect_mesh', {'path': 'relative.stl'}),
                await c.call_tool('inspect_mesh', {'path': str(tmp_path / 'gone.stl')}),
                await c.call_tool('job_status', {'job_id': '../../etc'}),
                await c.call_tool('fusion_handoff', {'path': str(tmp_path / 'other.py')})]
    (tmp_path / 'other.py').write_text('print(1)')
    out = drive(cfg, go)
    assert all(r.is_error for r in out)
    assert 'not a mesh file' in out[0].content[0].text
    assert 'absolute path' in out[1].content[0].text
    assert 'no mesh file' in out[2].content[0].text
    assert 'not a job id' in out[3].content[0].text
    assert 'not in an output folder' in out[4].content[0].text


def test_root_restricts_reading(tmp_path, part):
    inside = tmp_path / 'shared'
    inside.mkdir()
    cfg = C.from_args(out=str(tmp_path / 'o'), root=str(inside))
    r = drive(cfg, lambda c: c.call_tool('inspect_mesh', {'path': part}))
    assert r.is_error and 'outside the folder' in r.content[0].text
    with pytest.raises(F.PathRefused):
        F.resolve_out_dir(cfg, str(tmp_path / 'elsewhere'), 'x')


def test_fusion_handoff(cfg, part):
    async def go(c):
        res = (await c.call_tool('convert_mesh', {'path': part, 'wait_s': 55})).structured_content
        while not res['finished']:
            res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
        script = (await c.call_tool('fusion_handoff', {'path': res['files']['fusion_script']})).structured_content
        step = (await c.call_tool('fusion_handoff', {'path': res['files']['step']})).structured_content
        return script, step
    script, step = drive(cfg, go)
    assert script['kind'] == 'script' and 'def run(' in script['python']
    assert step['kind'] == 'step_importer' and 'createSTEPImportOptions' in step['python']
    compile(step['python'], 'importer', 'exec')


def test_resources_and_prompts(cfg, tmp_path):
    from PIL import Image
    png = tmp_path / 'drawing.png'
    Image.new('RGB', (200, 100), 'white').save(png)

    async def go(c):
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        schema = await c.read_resource('stltosolid://blueprint/recipe-schema')
        guide = await c.read_resource('stltosolid://guide/which-tool')
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        reading = await c.get_prompt('read-drawing', {'drawing_path': str(png)})
        conv = await c.get_prompt('convert-for-fusion', {'path': '/a/b.stl'})
        return uris, schema, guide, prompts, reading, conv
    uris, schema, guide, prompts, reading, conv = drive(cfg, go)
    assert {'stltosolid://guide/which-tool', 'stltosolid://blueprint/recipe-schema'} <= uris
    assert 'features' in json.loads(schema.contents[0].text)['properties']
    assert 'convert_mesh' in guide.contents[0].text
    assert prompts == {'convert-for-fusion', 'read-drawing', 'xray-to-fusion', 'send-to-fusion'}
    assert [m.content.type for m in reading.messages] == ['text', 'image', 'text']
    assert 'build_recipe' in reading.messages[0].content.text
    assert '/a/b.stl' in conv.messages[0].content.text


def test_a_big_drawing_is_shrunk_for_the_prompt(tmp_path):
    import io
    import random
    from PIL import Image
    from stl_to_solid_mcp.server import MAX_PROMPT_IMAGE_BYTES, MAX_PROMPT_IMAGE_SIDE, _prompt_image
    p = tmp_path / 'huge.png'
    Image.frombytes('RGB', (3000, 1500), random.randbytes(3000 * 1500 * 3)).save(p)
    assert p.stat().st_size > MAX_PROMPT_IMAGE_BYTES
    data, mime = _prompt_image(str(p))
    assert mime == 'image/jpeg'
    with Image.open(io.BytesIO(base64.b64decode(data))) as im:
        assert max(im.size) == MAX_PROMPT_IMAGE_SIDE


def test_real_stdio_subprocess(tmp_path, part):
    """Real pipes: nothing the server or its workers print may reach the
    protocol stream, or the client fails to parse it."""
    params = StdioServerParameters(command=sys.executable, args=['-m', 'stl_to_solid_mcp', '--out', str(tmp_path / 'o')],
                                   cwd=str(tmp_path))

    async def go():
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            res = (await c.call_tool('convert_mesh', {'path': part, 'wait_s': 55})).structured_content
            while not res['finished']:
                res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
            return names, res
    names, res = asyncio.run(go())
    assert 'convert_mesh' in names and 'build_cadquery' not in names
    assert res['ok'] and os.path.exists(res['files']['step'])


def test_worker_dies_with_a_killed_server(tmp_path, cfg, part):
    """A client can kill the server at any moment. The worker's stdin pipe
    closes with it, so a long job does not run on with nobody waiting."""
    code = (
        'import sys, json, os, time\n'
        'from stl_to_solid_mcp import config as C\n'
        'from stl_to_solid.service import jobs\n'
        f'jobs.DATA_DIR = {cfg.jobs_dir!r}\n'
        'os.makedirs(jobs.DATA_DIR, exist_ok=True)\n'
        'os.environ[jobs.WORKER_WATCH_ENV] = "1"\n'
        'import subprocess\n'
        'p = subprocess.Popen([sys.executable, "-c", "import time; from stl_to_solid.service import worker; '
        'worker._die_with_parent(); print(\'up\', flush=True); time.sleep(120)"], '
        'stdout=subprocess.PIPE, text=True, **jobs.worker_stdin())\n'
        'print(p.stdout.readline().strip(), p.pid, flush=True)\n'
        'time.sleep(120)\n')
    env = dict(os.environ, **{'PYTHONPATH': os.getcwd()})
    parent = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, env=env)
    line = parent.stdout.readline().split()
    assert line[0] == 'up'
    child = int(line[1])
    assert _alive(child)
    parent.kill()
    parent.wait()
    for _ in range(40):
        if not _alive(child):
            return
        time.sleep(0.25)
    pytest.fail('the worker outlived the server')


def _alive(pid):
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
        return str(pid) in out
    # the child's own parent is gone by now, so nothing reaps it: a zombie counts as gone
    out = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith('Z')


def test_a_worker_crash_is_a_sentence(cfg, tmp_path, monkeypatch):
    bad = tmp_path / 'bad.stl'
    bad.write_bytes(b'this is not a mesh at all')
    r = drive(cfg, lambda c: c.call_tool('inspect_mesh', {'path': str(bad)}))
    assert r.is_error and 'Traceback' not in r.content[0].text


def test_runner_reports_a_timeout(cfg, part):
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    with pytest.raises(runner.CallFailed, match='longer than'):
        runner.call(cfg, 'inspect_mesh', part, timeout=0.01)


def test_command_forms(tmp_path):
    """Both ways in reach the server, and a missing extra is a sentence."""
    from stl_to_solid import __version__
    for cmd in ([sys.executable, '-m', 'stl_to_solid_mcp', '--version'],
                [sys.executable, '-c', 'import sys; from stl_to_solid.pipeline import main; '
                 'sys.argv = ["stltosolid", "mcp", "--version"]; main()']):
        out = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
        assert out.stdout.strip() == __version__, out.stderr
    blocked = ('import sys; sys.modules["mcp"] = None; sys.modules["mcp.server"] = None; '
               'sys.modules["mcp.server.mcpserver"] = None\n'
               'import builtins; real = builtins.__import__\n'
               'def fake(name, *a, **k):\n'
               '    if name.split(".")[0] == "mcp": raise ModuleNotFoundError("no", name="mcp")\n'
               '    return real(name, *a, **k)\n'
               'builtins.__import__ = fake\n'
               'from stl_to_solid_mcp.cli import main; main([])')
    out = subprocess.run([sys.executable, '-c', blocked], capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode != 0 and 'stltosolid[mcp]' in out.stderr and 'Traceback' not in out.stderr


def test_registry_entry_matches_the_package():
    """server.json and the bundle state a version and a command: they must
    be this package's, or a release publishes something that cannot start."""
    from stl_to_solid import __version__
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'server.json')) as f:
        doc = json.load(f)
    assert doc['version'] == __version__
    pypi = next(p for p in doc['packages'] if p['registryType'] == 'pypi')
    assert pypi['version'] == __version__ and pypi['identifier'] == 'stltosolid'
    assert pypi['packageArguments'][0]['value'] == 'mcp'
    with open(os.path.join(root, 'README.md')) as f:
        assert 'mcp-name: ' + doc['name'] in f.read()
    with open(os.path.join(root, 'packaging', 'mcpb', 'manifest.json')) as f:
        manifest = json.load(f)
    assert manifest['version'] == __version__
    tools = {t.name for t in asyncio.run(_list_tools())}
    assert {t['name'] for t in manifest['tools']} == tools


async def _list_tools():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        async with Client(build_server(C.from_args(out=d, allow_code=True))) as c:
            return (await c.list_tools()).tools
