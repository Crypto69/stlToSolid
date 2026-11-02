"""Smoke test of an installed MCP server over real stdio, the way a client
starts it: list the tools, convert a small part, read the STEP back.

    uv run --python 3.12 --with "mcp>=2.3,<3" python tools/mcp_smoke.py -- uvx --from "stltosolid[mcp]" stltosolid-mcp

Everything after `--` is the command that starts the server. Stdlib plus the
MCP client only, so it can run outside the server's own environment.
"""
import asyncio
import os
import sys
import tempfile
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters

TRIS = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
VERTS = [(0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0), (0, 0, 5), (20, 0, 5), (20, 10, 5), (0, 10, 5)]
WANT = {'inspect_mesh', 'convert_mesh', 'job_status', 'cancel_job', 'suggest_loft_axis', 'trace_section',
        'plan_xray', 'xray_fusion_script', 'inspect_step', 'preview_recipe', 'build_recipe', 'fusion_handoff'}


def write_box(path):
    with open(path, 'w') as f:
        f.write('solid box\n')
        for tri in TRIS:
            f.write(' facet normal 0 0 0\n  outer loop\n')
            for i in tri:
                f.write('   vertex %g %g %g\n' % VERTS[i])
            f.write('  endloop\n endfacet\n')
        f.write('endsolid box\n')


async def run(cmd, d):
    stl = os.path.join(d, 'box.stl')
    write_box(stl)
    env = {'STLTOSOLID_OUT': os.path.join(d, 'out')}
    t0 = time.time()
    async with Client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)) as c:
        print(f'connected in {time.time() - t0:.1f} s')
        names = {t.name for t in (await c.list_tools()).tools}
        missing = WANT - names
        assert not missing, f'tools missing: {missing}'
        info = (await c.call_tool('inspect_mesh', {'path': stl})).structured_content
        assert info['triangles'] == 12, info
        res = (await c.call_tool('convert_mesh', {'path': stl, 'wait_s': 50})).structured_content
        while not res['finished']:
            res = (await c.call_tool('job_status', {'job_id': res['job_id'], 'wait_s': 50})).structured_content
        assert res['ok'], res
        step = (await c.call_tool('inspect_step', {'path': res['files']['step']})).structured_content
        assert step['solids'] == 1 and step['closed'] is True, step
        pic = await c.call_tool('trace_section', {'path': stl, 'axis': 'z'})
        assert [b.type for b in pic.content] == ['text', 'image'], pic.content
    print(f"ok: {len(names)} tools, {res['report']['mode']}, {step['faces']} faces, {time.time() - t0:.1f} s in all")


def main():
    if '--' not in sys.argv:
        sys.exit(__doc__)
    cmd = sys.argv[sys.argv.index('--') + 1:]
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(run(cmd, d))


if __name__ == '__main__':
    main()
