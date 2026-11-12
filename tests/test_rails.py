"""Rails: 90-degree cuts along an X-Ray stack, pinned to the fitted profile
curves so a Fusion loft accepts them (a rail must touch every profile)."""
import math

import numpy as np
import pytest
import trimesh

from . import synth


def _stack(m, zs, tol=0.08):
    from stl_to_solid.section_fit import fit_section
    return [fit_section(m.vertices, m.faces, [0, 0, z], [0, 0, 1], tol=tol) for z in zs]


def _on_profile(pt, sec2d):
    """Distance from a 3-D point to the drawn outer curves of a 2-D traced
    section (splines as the polyline through their fit points)."""
    from stl_to_solid.section_fit import _open_polyline_dev, prim_points
    o, u, v = (np.asarray(sec2d[k], float) for k in ('origin', 'u', 'v'))
    p = np.asarray(pt, float) - o
    xy = np.array([[p @ u, p @ v]])
    best = math.inf
    for outer, _ in sec2d['loops']:
        if isinstance(outer, dict):
            best = min(best, abs(np.linalg.norm(xy[0] - outer['center']) - outer['r']))
            continue
        for q in outer:
            if q['type'] == 'arc':
                # the drawn three-point arc
                from stl_to_solid.profile_fit import _arc_mid
                p0, pm, p1 = (np.asarray(x, float) for x in (q['p0'], _arc_mid(q), q['p1']))
                d1, d2 = pm - p0, p1 - p0
                den = 2.0 * (d1[0] * d2[1] - d1[1] * d2[0])
                c = p0 + np.array([d2[1] * (d1 @ d1) - d1[1] * (d2 @ d2),
                                   d1[0] * (d2 @ d2) - d2[0] * (d1 @ d1)]) / den
                best = min(best, abs(np.linalg.norm(xy[0] - c) - np.linalg.norm(p0 - c)))
            else:
                poly = np.vstack([prim_points([q], arc_step_deg=0.5), [q['p1']]]) if q['type'] == 'line' \
                    else np.asarray(q['pts'], float)
                best = min(best, _open_polyline_dev(xy, poly))
    return best


@pytest.fixture(scope='module')
def obround(tmp_path_factory):
    p = synth.export(synth.obround(), str(tmp_path_factory.mktemp('r') / 'ob.stl'))
    return trimesh.load(p, force='mesh')


def test_rail_planes_are_the_stack_frame():
    from stl_to_solid.rails import rail_planes
    from stl_to_solid.section_fit import plane_basis
    u, v, _ = plane_basis([0, 0, 1])
    pl = rail_planes([0, 0, 1], [1, 2, 3], 2)
    assert np.allclose(pl[0][1], u) and np.allclose(pl[1][1], v)
    assert all(np.allclose(o, [1, 2, 3]) for o, _ in pl)
    assert rail_planes([0, 0, 1], [0, 0, 0], 0) == []


def test_obround_has_four_complete_rails(obround):
    """Obround along Z: the two rail planes each cut the outer wall twice
    (4 complete rails) and the slot wall twice (flagged, not complete).
    Every crossing lies on its profile's drawn curve; the ends are on the
    first and last planes."""
    from stl_to_solid.rails import trace_rails
    zs = np.arange(0.5, 7.6, 1.0)
    secs = _stack(obround, zs)
    rails = trace_rails(obround.vertices, obround.faces, [0, 0, 1], obround.bounding_box.centroid,
                        secs, tol=0.08)
    done = [r for r in rails if r['complete']]
    assert len(done) == 4 and len(rails) == 8
    assert all(r['note'] for r in rails if not r['complete'])
    for r in done:
        assert [k for k, _ in r['crossings']] == list(range(len(zs)))
        for k, pt in r['crossings']:
            assert _on_profile(pt, secs[k]) < 1e-9
        assert r['pts'][0] == r['crossings'][0][1] and r['pts'][-1] == r['crossings'][-1][1]
        assert abs(r['pts'][0][2] - zs[0]) < 1e-9 and abs(r['pts'][-1][2] - zs[-1]) < 1e-9
        # every crossing is one of the rail's own fit points
        for _, pt in r['crossings']:
            assert pt in r['pts']


def test_wavy_profile_crossing_goes_into_the_spline():
    """A rippled tube (splines, not lines/arcs): the crossing is inserted
    into the spline's fit points, so the spline Fusion fits passes through
    it."""
    from stl_to_solid.rails import trace_rails
    t = np.linspace(0, 2 * np.pi, 2000, endpoint=False)
    r = 10 + 0.25 * np.sin(80 * t + 0.3)
    ring = np.c_[r * np.cos(t), r * np.sin(t)]
    zs = np.linspace(0, 6, 7)
    V = np.vstack([np.c_[ring, np.full(len(ring), z)] for z in (-1.0, 7.0)])
    n = len(ring)
    F = []
    for i in range(n):
        j = (i + 1) % n
        F += [[i, j, n + j], [i, n + j, n + i]]
    m = trimesh.Trimesh(V, np.asarray(F), process=False)
    secs = _stack(m, zs, tol=0.02)
    assert any(p['type'] == 'spline' for outer, _ in secs[0]['loops'] for p in outer)
    rails = trace_rails(m.vertices, m.faces, [0, 0, 1], [0, 0, 3.0], secs, tol=0.02)
    done = [r for r in rails if r['complete']]
    assert len(done) == 4
    from stl_to_solid.section_fit import _open_polyline_dev
    on_spline = 0
    for r in done:
        for k, pt in r['crossings']:
            assert _on_profile(pt, secs[k]) < 1e-9
            p2 = np.asarray(pt)[:2]
            for outer, _ in secs[k]['loops']:
                for p in outer:
                    if p['type'] != 'spline':
                        continue
                    q = np.asarray(p['pts'], float)
                    if _open_polyline_dev(p2[None], q) < 1e-9:
                        # on this spline: then it is one of its fit points
                        assert np.linalg.norm(q - p2, axis=1).min() < 1e-12
                        on_spline += 1
    assert on_spline >= 4


def test_side_bump_is_flagged_partial(tmp_path):
    """A part whose rail-plane cut leaves the profile span sideways (a
    stepped shaft: the step's shoulder runs across the axis) is flagged
    partial where a profile is missed or crossed twice."""
    from stl_to_solid.rails import trace_rails
    p = synth.export(synth.stepped_shaft(), str(tmp_path / 'shaft.stl'))
    m = trimesh.load(p, force='mesh')
    zs = np.arange(1.0, 44.0, 3.0)
    secs = _stack(m, zs)
    rails = trace_rails(m.vertices, m.faces, [0, 0, 1], m.bounding_box.centroid, secs, tol=0.08)
    assert rails and all(r['complete'] for r in rails)      # the cut goes round the steps
    for r in rails:
        for k, pt in r['crossings']:
            assert _on_profile(pt, secs[k]) < 1e-9
    # a rail planned past the part's end misses the profiles there
    secs_long = secs + _stack(m, [zs[-1] + 30.0])
    assert not secs_long[-1]['loops']
    rails = trace_rails(m.vertices, m.faces, [0, 0, 1], m.bounding_box.centroid, secs_long, tol=0.08)
    assert rails and not any(r['complete'] for r in rails)
    assert all('section' in r['note'] for r in rails)


def test_rounding_keeps_the_crossing_on_the_line(obround):
    """At 6 decimals (the rail scripts' rounding) a crossing still sits
    within 1e-6 mm of its profile line."""
    from stl_to_solid.fusion_export import _xray_round
    from stl_to_solid.rails import trace_rails
    secs = _stack(obround, [0.5, 3.3333333, 7.5])
    rails = trace_rails(obround.vertices, obround.faces, [0, 0, 1], obround.bounding_box.centroid,
                        secs, tol=0.08)
    for r in rails:
        if r['complete']:
            for k, pt in _xray_round(r['crossings'], 6):
                assert _on_profile(pt, secs[k]) < 1e-6


# --- the X-Ray script with rails --------------------------------------------

class _Coll(list):
    def add(self, x):
        self.append(x)

    @property
    def count(self):
        return len(self)

    def item(self, i):
        return self[i]


class _Prof:
    def __init__(self, area):
        self.area = area

    def areaProperties(self):
        from types import SimpleNamespace
        return SimpleNamespace(area=self.area)


class _Lofts:
    def __init__(self, refuse=0):
        self.refuse, self.made, self.tries = refuse, [], []

    def createInput(self, op):
        from types import SimpleNamespace
        li = SimpleNamespace(op=op, loftSections=_Coll(), isSolid=False)
        li.centerLineOrRails = SimpleNamespace(rails=[], addRail=lambda c: li.centerLineOrRails.rails.append(c))
        return li

    def add(self, li):
        self.tries.append(len(li.centerLineOrRails.rails))
        if self.refuse:
            self.refuse -= 1
            raise RuntimeError('5 : rail does not touch all profiles')
        self.made.append(li)
        return li


def _rails_ns(monkeypatch, rails, loft=True):
    from types import SimpleNamespace
    from stl_to_solid.fusion_export import emit_fusion_xray_script
    from .test_xray import _fake_adsk
    _fake_adsk(monkeypatch)
    txt = emit_fusion_xray_script([], title='t', rails=rails, loft=loft)
    ns = {}
    exec(txt, ns)
    planes = []

    def plane(root, origin, normal):
        pl = SimpleNamespace(name='', origin=origin, normal=normal)
        planes.append(pl)
        return pl, 1.0
    ns['_plane'] = plane
    return ns, planes


def _fake_root(lofts):
    from types import SimpleNamespace
    sketches = []

    def add(pl):
        splines = []
        sk = SimpleNamespace(name='', plane=pl, splines=splines, modelToSketchSpace=lambda p: p)
        sk.sketchCurves = SimpleNamespace(sketchFittedSplines=SimpleNamespace(
            add=lambda pc: (splines.append(list(pc)), ('spline', len(splines)))[1]))
        sketches.append(sk)
        return sk
    return SimpleNamespace(sketches=SimpleNamespace(add=add),
                           features=SimpleNamespace(loftFeatures=lofts)), sketches


def _drawn(n):
    from types import SimpleNamespace
    return [(SimpleNamespace(name='xray %d' % i, profiles=_Coll([_Prof(1.0), _Prof(0.25)])),
             {'loops': [1], 'areas_mm2': [100.0]}) for i in range(n)]


def _rail(i, normal, complete=True):
    return {'name': 'rail %d/4' % (i + 1), 'plane': {'origin': [0, 0, 0], 'normal': normal},
            'pts': [[0, 6, 0.5], [0, 6, 7.5]], 'complete': complete, 'note': '' if complete else 'section 2: missed'}


def test_xray_script_rails_and_loft(monkeypatch):
    """Four rails on two planes: two rail sketches, one spline per rail, a
    loft through the largest material profile of every section along the
    complete rails, and the closing notes say so."""
    rails = [_rail(0, [1, 0, 0]), _rail(1, [1, 0, 0]), _rail(2, [0, 1, 0]), _rail(3, [0, 1, 0], False)]
    ns, planes = _rails_ns(monkeypatch, rails)
    lofts = _Lofts()
    root, sketches = _fake_root(lofts)
    notes = []
    ns['_rails_and_loft'](root, _drawn(3), notes)
    assert len(planes) == 2 and [s.name for s in sketches] == ['rails 1', 'rails 2']
    assert [len(s.splines) for s in sketches] == [2, 2]
    assert len(lofts.made) == 1 and lofts.tries == [3]
    li = lofts.made[0]
    assert li.isSolid and [p.area for p in li.loftSections] == [1.0, 1.0, 1.0]
    assert notes[0] == 'Loft: one new body with 3 rail(s)'
    assert 'Rails: 4 drawn, 3 complete' in notes[1] and 'rail 4/4' in notes[1]


def test_xray_script_loft_falls_back_without_rails(monkeypatch):
    rails = [_rail(0, [1, 0, 0])]
    ns, _ = _rails_ns(monkeypatch, rails)
    lofts = _Lofts(refuse=1)
    root, _ = _fake_root(lofts)
    notes = []
    ns['_rails_and_loft'](root, _drawn(2), notes)
    assert lofts.tries == [1, 0] and len(lofts.made) == 1
    assert notes[0].startswith('Loft: one new body without rails (RuntimeError: 5 : rail does not')
    # refused both ways: the sketches stay, the message says why
    lofts = _Lofts(refuse=2)
    root, _ = _fake_root(lofts)
    notes = []
    ns['_rails_and_loft'](root, _drawn(2), notes)
    assert not lofts.made and notes[0].startswith('WARNING: Fusion refused the loft')


def test_xray_script_loft_skips(monkeypatch):
    ns, _ = _rails_ns(monkeypatch, [_rail(0, [1, 0, 0])])
    for drawn, why in ((_drawn(61), 'more than 60'), (_drawn(1), 'at least two'),
                       (_drawn(2)[:1] + [(_drawn(1)[0][0], {'loops': []})], 'no closed outline')):
        lofts = _Lofts()
        root, _ = _fake_root(lofts)
        notes = []
        ns['_rails_and_loft'](root, drawn, notes)
        assert not lofts.tries and any(why in s for s in notes), notes
    ns, _ = _rails_ns(monkeypatch, [_rail(0, [1, 0, 0])], loft=False)
    lofts = _Lofts()
    root, _ = _fake_root(lofts)
    notes = []
    ns['_rails_and_loft'](root, _drawn(3), notes)
    assert not lofts.tries and notes == ['Rails: 1 drawn, 1 complete']


def test_xray_script_with_rails_hooks_run_and_keeps_default(monkeypatch):
    from stl_to_solid.fusion_export import _XRAY_RUNTIME, emit_fusion_xray_script
    sec = [{'origin': [0, 0, 1.123456789], 'normal': [0, 0, 1], 'loops': [], 'open': []}]
    plain = emit_fusion_xray_script(sec, title='t')
    assert 'RAILS' not in plain and '_DRAWN' not in plain and '1.1235' in plain
    txt = emit_fusion_xray_script(sec, title='t', rails=[_rail(0, [1, 0, 0])], loft=True)
    assert '1.123457' in txt and 'LOFT = True' in txt and 'RAILS = [' in txt
    assert txt.count('_DRAWN.append((sk, sec))') == 1 and txt.count('_rails_and_loft(root, _DRAWN, notes)') == 1
    assert _XRAY_RUNTIME in plain
    compile(txt, 'xray.py', 'exec')


def test_xray_service_script_with_rails(obround, tmp_path):
    from stl_to_solid.service import xray
    p = str(tmp_path / 'ob.stl')
    obround.export(p)
    offs = [-3.5, -2.0, 0.0, 2.0, 3.5]
    s = xray.script(p, 'z', -3.5, 3.5, 2.0, offs, rails=True, loft=True, extrude=True)
    assert s['rails'] == {'count': 8, 'complete': 4}
    assert s['name'].endswith('_loft.py') and "'extrude_mm': None" in s['text']
    assert s['text'].count("'complete': True") == 4 and 'LOFT = True' in s['text']
    assert any('8 rails, 4 touching every profile' in n for n in s['notes'])
    plain = xray.script(p, 'z', -3.5, 3.5, 2.0, offs)
    assert 'RAILS' not in plain['text'] and 'rails' in plain and plain['rails']['count'] == 0
    prev = xray.rails_preview(p, 'z', -3.5, 3.5)
    assert prev['kind'] == 'rail' and len(prev['polylines']) == 8


def test_xray_rails_api(obround, tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv('STLTOSOLID_DATA', str(tmp_path / 'data'))
    from backend import jobs, main
    importlib.reload(jobs)
    p = str(tmp_path / 'ob.stl')
    obround.export(p)
    with TestClient(main.app) as c:
        with open(p, 'rb') as fh:
            job = c.post('/api/jobs', files={'file': ('ob.stl', fh, 'application/octet-stream')}).json()['id']
        r = c.get(f'/api/jobs/{job}/xray-rails', params={'axis': 'z', 'from': -3, 'to': 3})
        assert r.status_code == 200 and len(r.json()['polylines']) == 8
        r = c.get(f'/api/jobs/{job}/xray-script', params={'axis': 'z', 'from': -3, 'to': 3, 'step': 1.5,
                                                          'rails': 'true', 'loft': 'true'})
        assert r.status_code == 200 and 'RAILS = [' in r.text and 'LOFT = True' in r.text
        r = c.get(f'/api/jobs/{job}/xray-script', params={'axis': 'z', 'from': -3, 'to': 3, 'step': 1.5,
                                                          'loft': 'true'})
        assert r.status_code == 200 and 'RAILS' not in r.text      # loft needs rails


def test_mcp_xray_script_with_rails(obround, tmp_path):
    pytest.importorskip('mcp')
    pytest.importorskip('PIL')
    import asyncio
    from mcp import Client
    from stl_to_solid_mcp import config as C
    from stl_to_solid_mcp.server import build_server
    cfg = C.from_args(out=str(tmp_path / 'out'))
    p = str(tmp_path / 'ob.stl')
    obround.export(p)

    async def go():
        async with Client(build_server(cfg)) as c:
            return await c.call_tool('xray_fusion_script', {'path': p, 'axis': 'z', 'from_mm': -3,
                                                            'to_mm': 3, 'step_mm': 1.5, 'rails': True,
                                                            'loft': True})
    r = asyncio.run(go()).structured_content
    assert r['rails'] == {'count': 8, 'complete': 4} and r['script'].endswith('_loft.py')
    with open(r['script']) as f:
        assert 'RAILS = [' in f.read()
