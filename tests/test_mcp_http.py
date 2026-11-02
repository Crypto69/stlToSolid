"""Remote mode: the token, the shared folder and its file routes."""
import asyncio
import os

import pytest

pytest.importorskip('mcp')
pytest.importorskip('PIL')
pytest.importorskip('httpx')

from starlette.testclient import TestClient                   # noqa: E402

from stl_to_solid_mcp import config as C                       # noqa: E402
from stl_to_solid_mcp.http import build_app, serve             # noqa: E402

from . import synth                                            # noqa: E402

AUTH = {'Authorization': 'Bearer s3cret'}


@pytest.fixture
def cfg(tmp_path):
    return C.from_args(out=str(tmp_path / 'share'), root=str(tmp_path / 'share'), http=True,
                       base_url='http://nas.local:8322')


@pytest.fixture
def web(cfg):
    os.makedirs(cfg.out_root, exist_ok=True)
    with TestClient(build_app(cfg, 's3cret'), base_url='http://nas.local:8322') as c:
        yield c


def test_every_route_but_health_needs_the_token(web):
    assert web.get('/healthz').status_code == 200
    assert web.get('/files/x.stl').status_code == 401
    assert web.post('/mcp', json={}).status_code == 401
    assert web.get('/files/x.stl', headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_upload_then_download(web, cfg, tmp_path):
    src = tmp_path / 'part.stl'
    synth.export(synth.plate_holes(), str(src))
    r = web.put('/files/part.stl', content=src.read_bytes(), headers=AUTH)
    assert r.status_code == 201 and r.json()['path'] == 'uploads/part.stl'
    assert r.json()['url'] == 'http://nas.local:8322/files/uploads/part.stl'
    assert web.put('/files/part.stl', content=b'x', headers=AUTH).json()['path'] == 'uploads/part-2.stl'
    got = web.get('/files/uploads/part.stl', headers=AUTH)
    assert got.status_code == 200 and got.content == src.read_bytes()


def test_file_routes_stay_inside_the_share(web, cfg):
    assert web.put('/files/evil.sh', content=b'x', headers=AUTH).status_code == 415
    assert web.get('/files/%2e%2e/%2e%2e/etc/passwd', headers=AUTH).status_code == 404
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    with open(os.path.join(cfg.jobs_dir, 'scratch.step'), 'w') as f:
        f.write('x')
    assert web.get('/files/.jobs/scratch.step', headers=AUTH).status_code == 404


def test_paths_are_relative_and_results_have_urls(cfg, tmp_path):
    """Over a real socket with a real client, since the streaming endpoint
    needs its session manager running."""
    import socket
    import threading
    import uvicorn
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    share = cfg.root
    os.makedirs(os.path.join(share, 'uploads'), exist_ok=True)
    synth.export(synth.plate_holes(), os.path.join(share, 'uploads', 'plate.stl'))
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(cfg, 's3cret'), host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass

    async def go():
        async with httpx2.AsyncClient(headers=AUTH, timeout=120) as http:
            async with Client(streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http)) as c:
                tools = {t.name for t in (await c.list_tools()).tools}
                listed = (await c.call_tool('list_files', {})).structured_content
                res = (await c.call_tool('convert_mesh', {'path': 'uploads/plate.stl', 'wait_s': 55})).structured_content
                while not res['finished']:
                    res = (await c.call_tool('job_status', {'job_id': res['job_id']})).structured_content
                outside = await c.call_tool('inspect_mesh', {'path': '../../etc/hosts.stl'})
                return tools, listed, res, outside
    try:
        tools, listed, res, outside = asyncio.run(go())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert 'list_files' in tools and 'build_cadquery' not in tools
    assert any(f['path'] == 'uploads/plate.stl' for f in listed['files'])
    assert res['ok'] and res['files']['step'] == 'plate/plate.step'
    assert res['file_urls']['step'] == 'http://nas.local:8322/files/plate/plate.step'
    assert outside.is_error and 'outside' in outside.content[0].text


def test_refuses_an_open_network_port_with_no_token(monkeypatch):
    monkeypatch.delenv('STLTOSOLID_MCP_TOKEN', raising=False)

    class Args:
        token = None
        no_auth = False
        host = '0.0.0.0'
    with pytest.raises(SystemExit, match='no token'):
        serve(Args)
