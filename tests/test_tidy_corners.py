"""Tidy corners for X-Ray / Mesh to Outline sections: tiny rounds turned
into sharp creases (sharpen), fillets solved tangent and flagged
(tidy), and the Fusion drawing that chains the curves through shared
sketch points with tangent constraints. All off by default: the plain
trace and the default X-Ray script must not change."""
import asyncio
import math

import numpy as np
import pytest
import trimesh

from . import synth


# --- synthetic outlines ----------------------------------------------------

def _rounded_rect(w, h, r, seg_deg=10, per_edge=12, noise=0.0, seed=1):
    """A rounded rectangle traced like a mesh section: each round as chords
    every `seg_deg`, every edge split into `per_edge` points, optional
    Gaussian noise (mm)."""
    pts = []
    for cx, cy, a0 in [(w - r, h - r, 0), (r, h - r, 90), (r, r, 180), (w - r, r, 270)]:
        if r <= 0:
            pts.append((cx, cy))
            continue
        n = max(1, int(round(90 / seg_deg)))
        for k in range(n + 1):
            a = math.radians(a0 + 90 * k / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    P = np.array(pts)
    P = np.vstack([np.linspace(P[i], P[(i + 1) % len(P)], per_edge, endpoint=False)
                   for i in range(len(P))])
    if noise:
        P = P + np.random.default_rng(seed).normal(0, noise, P.shape)
    return P


def _turns(prims, closed=True):
    from stl_to_solid.section_fit import _end_dir
    n = len(prims)
    out = []
    for i in range(n if closed else n - 1):
        a, b = prims[i], prims[(i + 1) % n]
        c = float(np.clip(_end_dir(a, True) @ _end_dir(b, False), -1, 1))
        out.append(math.degrees(math.acos(c)))
    return out


def _kinds(prims):
    return [p['type'] for p in prims]


# --- engine ----------------------------------------------------------------

def test_fit_loop_tidy_rounded_rect_is_tangent(tmp_path):
    """The rounded-rect mesh section: 4 lines + 4 arcs, every junction
    shared exactly and every fillet tangent to its walls, all 8 flagged."""
    from stl_to_solid.section_fit import fit_loop, junction_gap, section_loops
    p = synth.export(synth.rounded_rect(), str(tmp_path / 'rr.stl'))
    m = trimesh.load(p, force='mesh')
    loops, _ = section_loops(m.vertices, m.faces, [0, 0, 0.0], [0, 0, 1])
    st = {}
    prims = fit_loop(loops[0][0], tol=0.08, tidy=True, stats=st)
    k = _kinds(prims)
    assert k.count('line') == 4 and k.count('arc') == 4 and 'spline' not in k
    assert junction_gap(prims) < 1e-9
    assert max(_turns(prims)) < 1e-4                           # degrees
    assert st['tangent'] == 8
    assert all(p.get('tan0') and p.get('tan1') for p in prims)


def test_tidy_solves_noisy_fillets():
    """A noisy rounded rect (0.01 mm noise, r = 1): the plain fit kinks
    up to 16 degrees at the fillets; tidy makes all 8 junctions tangent."""
    from stl_to_solid.section_fit import fit_loop, junction_gap
    xy = _rounded_rect(20, 10, 1.0, noise=0.01)
    plain = fit_loop(xy, tol=0.08)
    assert max(_turns(plain)) > 5
    st = {}
    prims = fit_loop(xy, tol=0.08, tidy=True, stats=st)
    assert st['tangent'] == 8 and max(_turns(prims)) < 0.05
    assert junction_gap(prims) < 1e-9


def test_sharpen_collapses_small_rounds():
    from stl_to_solid.section_fit import fit_loop
    st = {}
    prims = fit_loop(_rounded_rect(20, 10, 0.1), tol=0.08, sharpen_mm=0.08, stats=st)
    assert _kinds(prims) == ['line'] * 4
    assert all(abs(t - 90) < 1e-6 for t in _turns(prims))
    corners = sorted(tuple(np.round(p['p0'], 6)) for p in prims)
    assert corners == [(0.0, 0.0), (0.0, 10.0), (20.0, 0.0), (20.0, 10.0)]


def test_sharpen_keeps_real_rounds():
    from stl_to_solid.section_fit import fit_loop
    st = {}
    prims = fit_loop(_rounded_rect(20, 10, 1.0), tol=0.08, sharpen_mm=0.08, stats=st)
    assert _kinds(prims).count('arc') == 4 and st.get('sharpened', 0) == 0


@pytest.mark.parametrize('r', [0.06, 0.1])
def test_sharpen_on_noisy_crumbs(r):
    """The probe case: 0.01 mm noise and a tiny round. The plain fit turns
    one wall into a spline (its chord ends sit on the rounds, inside the
    wall); with tidy + sharpen it is four lines meeting square."""
    from stl_to_solid.section_fit import fit_loop
    xy = _rounded_rect(20, 10, r, noise=0.01)
    prims = fit_loop(xy, tol=0.08, tidy=True, sharpen_mm=0.08, stats={})
    assert _kinds(prims) == ['line'] * 4
    assert all(abs(t - 90) < 0.5 for t in _turns(prims))


def test_tidy_open_chain_keeps_free_ends():
    """An open L with a fillet: the fillet is solved tangent, the two free
    line ends stay exactly where the section put them."""
    from stl_to_solid.section_fit import fit_loop
    a = np.c_[np.zeros(40), np.linspace(10, 2, 40, endpoint=False)]
    t = np.radians(np.linspace(180, 270, 12, endpoint=False))
    arc = np.c_[2 + 2 * np.cos(t), 2 + 2 * np.sin(t)]
    b = np.c_[np.linspace(2, 10, 40), np.zeros(40)]
    xy = np.vstack([a, arc, b])
    xy[1:-1] += np.random.default_rng(3).normal(0, 0.005, xy[1:-1].shape)
    st = {}
    prims = fit_loop(xy, tol=0.08, tidy=True, closed=False, stats=st)
    assert prims[0]['type'] == 'line' and prims[-1]['type'] == 'line'
    assert np.array_equal(prims[0]['p0'], xy[0]) and np.array_equal(prims[-1]['p1'], xy[-1])
    assert st['tangent'] == 2 and max(_turns(prims, closed=False)) < 0.05


def test_tidy_reverts_runaway_fillet():
    """A solve that would move a junction 0.9 mm (two nearly parallel
    lines whose crossing is far from where they meet) is put back."""
    from stl_to_solid.section_fit import tidy_junctions
    A = {'type': 'line', 'p0': np.array([0.0, 0.0]), 'p1': np.array([10.0, 0.0])}
    B = {'type': 'line', 'p0': np.array([10.0, 0.05]), 'p1': np.array([20.0, 0.6])}
    st = {}
    tidy_junctions([A, B], 0.08, closed=False, stats=st)
    assert st == {'tidy_reverted': 1}
    assert np.array_equal(A['p1'], [10.0, 0.0]) and np.array_equal(B['p0'], [10.0, 0.05])


def test_sharpen_needs_a_real_turn():
    """Two walls in line (a step of a few hundredths) are not a corner."""
    from stl_to_solid.section_fit import sharpen_corners
    A = {'type': 'line', 'p0': np.array([0.0, 0.0]), 'p1': np.array([10.0, 0.0])}
    crumb = {'type': 'line', 'p0': np.array([10.0, 0.0]), 'p1': np.array([10.1, 0.02])}
    B = {'type': 'line', 'p0': np.array([10.1, 0.02]), 'p1': np.array([20.0, 0.02])}
    prims = [A, crumb, B]
    sharpen_corners(prims, 0.08, 0.08, closed=False)
    assert len(prims) == 3


def test_tidy_off_is_unchanged():
    """Default arguments and explicit tidy=False give the same trace, with
    no tangent flags and the new counters at zero."""
    from .test_pipeline import _sample
    from stl_to_solid.section_fit import section_preview
    m = trimesh.load(_sample('fixed-rc-n2-360.stl'), force='mesh')
    m.apply_scale(0.1)
    o = [0, 0, m.bounding_box.centroid[2] - 26.0]
    a = section_preview(m.vertices, m.faces, o, [0, 0, 1], tol=0.08)
    b = section_preview(m.vertices, m.faces, o, [0, 0, 1], tol=0.08, tidy=False, sharpen_mm=0.0)
    assert a['stats'] == b['stats'] and a['polylines'] == b['polylines']
    assert a['stats']['tangent'] == a['stats']['sharpened'] == a['stats']['tidy_reverted'] == 0
    flat = [p for outer, holes in a['loops'] for loop in [outer] + holes
            if not isinstance(loop, dict) for p in loop]
    assert flat and not any('tan0' in p or 'tan1' in p for p in flat)


def test_tidy_on_a_real_section_stays_closed_and_close():
    """The RC-N2 cut with tidy + sharpen: every junction still exact, the
    worst miss no worse than the plain fit's plus a hair, tangents found."""
    from .test_pipeline import _sample
    from stl_to_solid.section_fit import fit_section
    m = trimesh.load(_sample('fixed-rc-n2-360.stl'), force='mesh')
    m.apply_scale(0.1)
    o = [0, 0, m.bounding_box.centroid[2] - 26.0]
    plain = fit_section(m.vertices, m.faces, o, [0, 0, 1], tol=0.08)['stats']
    st = fit_section(m.vertices, m.faces, o, [0, 0, 1], tol=0.08, tidy=True, sharpen_mm=0.08)['stats']
    assert st['junction_gap'] < 1e-9
    assert st['dev_max'] <= max(plain['dev_max'], 0.08) + 0.01, (plain, st)
    assert st['tangent'] >= 10


def test_lifted_prims_carry_the_flags():
    from stl_to_solid.section_fit import fit_loop, lift_prims
    prims = fit_loop(_rounded_rect(20, 10, 1.0), tol=0.08, tidy=True)
    frame = (np.zeros(3), np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]))
    lifted = lift_prims(prims, frame)
    assert all(q['tan0'] is True and q['tan1'] is True for q in lifted)
    from stl_to_solid.fusion_export import _xray_round
    assert all(q['tan0'] is True for q in _xray_round(lifted))


# --- the X-Ray script --------------------------------------------------------

class _Pt:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    def distanceTo(self, o):
        return math.dist((self.x, self.y, self.z), (o.x, o.y, o.z))


class _SP:
    """A sketch point."""
    def __init__(self, g):
        self.geometry = g


def _sp(a):
    return a if isinstance(a, _SP) else _SP(a)


def _geom(a):
    return a.geometry if isinstance(a, _SP) else a


class _Curve:
    def __init__(self, kind, a, b, mid=None):
        self.kind = kind
        self.startSketchPoint, self.endSketchPoint = _sp(a), _sp(b)
        if mid is not None:
            # Fusion keeps arcs counter-clockwise: a clockwise one comes
            # back with its ends the other way round
            A, M, B = _geom(a), mid, _geom(b)
            cross = (M.x - A.x) * (B.y - M.y) - (M.y - A.y) * (B.x - M.x)
            if cross < 0:
                self.startSketchPoint, self.endSketchPoint = self.endSketchPoint, self.startSketchPoint


class _Sketch:
    def __init__(self):
        self.curves, self.tangents = [], []
        s = self

        def add(c):
            s.curves.append(c)
            return c

        self.sketchCurves = type('C', (), {})()
        self.sketchCurves.sketchLines = type('L', (), {'addByTwoPoints': lambda _, a, b: add(_Curve('line', a, b))})()
        self.sketchCurves.sketchArcs = type('A', (), {
            'addByThreePoints': lambda _, a, m, b: add(_Curve('arc', a, b, mid=m))})()
        self.sketchCurves.sketchFittedSplines = type('S', (), {'add': lambda _, pc: add(_Curve('spline', pc[0], pc[-1]))})()
        self.sketchCurves.sketchCircles = type('O', (), {'addByCenterRadius': lambda _, c, r: add(_Curve('circle', c, c))})()
        self.geometricConstraints = type('G', (), {'addTangent': lambda _, a, b: s.tangents.append((a, b))})()

    def modelToSketchSpace(self, p):
        return p


def _tidy_runtime(monkeypatch):
    from types import SimpleNamespace
    from stl_to_solid.fusion_export import emit_fusion_xray_script
    from .test_xray import _fake_adsk
    adsk = _fake_adsk(monkeypatch)
    adsk.core.Point3D = SimpleNamespace(create=staticmethod(_Pt))
    txt = emit_fusion_xray_script([], title='t', tidy=True)
    ns = {}
    exec(txt, ns)
    return ns


def test_xray_script_default_is_byte_identical():
    """No tidy: not one byte of the tidy runtime in the script."""
    from stl_to_solid.fusion_export import _XRAY_RUNTIME, emit_fusion_xray_script
    sec = [{'origin': [0, 0, 1.5], 'normal': [0, 0, 1], 'loops': [], 'open': []}]
    a = emit_fusion_xray_script(sec, title='t', notes=['n'])
    assert a == emit_fusion_xray_script(sec, title='t', notes=['n'], tidy=False)
    assert 'draw_prims_tidy' not in a and 'addTangent' not in a and 'draw_prims_tidy' not in _XRAY_RUNTIME
    b = emit_fusion_xray_script(sec, title='t', notes=['n'], tidy=True)
    assert b.startswith(a) and 'draw_prims_tidy' in b
    compile(b, 'xray.py', 'exec')


def test_xray_script_tidy_shares_points_and_adds_tangents(monkeypatch):
    """The noisy rounded rect, lifted and drawn by the script's runtime:
    each curve starts on the previous curve's end sketch point (clockwise
    arcs included), the last ends on the first's start, and there is one
    tangent constraint per flagged junction."""
    from stl_to_solid.section_fit import fit_loop, lift_prims
    ns = _tidy_runtime(monkeypatch)
    st = {}
    prims = fit_loop(_rounded_rect(20, 10, 1.0, noise=0.01)[::-1], tol=0.08, tidy=True, stats=st)
    frame = (np.zeros(3), np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]))
    sec = {'loops': [(lift_prims(prims, frame), [])], 'open': []}
    sk, notes = _Sketch(), {}
    n = ns['draw_section'](sk, sec, notes)
    assert n == len(prims) == len(sk.curves)
    assert st['tangent'] == 8 and notes['tangent'] == 8 and len(sk.tangents) == 8
    assert not notes.get('tangent_failed')

    def ends(c, p):
        a, b = c.startSketchPoint, c.endSketchPoint
        q0 = _Pt(*[v / 10.0 for v in (p['p0'] if p['type'] != 'spline' else p['pts'][0])])
        return (a, b) if a.geometry.distanceTo(q0) <= b.geometry.distanceTo(q0) else (b, a)
    path = [ends(c, p) for c, p in zip(sk.curves, sec['loops'][0][0])]
    for (s0, e0), (s1, e1) in zip(path, path[1:]):
        assert e0 is s1
    assert path[-1][1] is path[0][0]
    assert any(c.kind == 'arc' for c in sk.curves)
    assert ns['NOTES'][-1] == 'Tidy corners: 8 tangent constraint(s) added'


def test_xray_script_tidy_falls_back_when_fusion_refuses(monkeypatch):
    """A curve refused on shared points is drawn from plain points (and a
    refused arc as lines); its tangent constraints are counted as failed."""
    from stl_to_solid.section_fit import fit_loop, lift_prims
    ns = _tidy_runtime(monkeypatch)
    prims = fit_loop(_rounded_rect(20, 10, 1.0), tol=0.08, tidy=True)
    frame = (np.zeros(3), np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]))
    lifted = lift_prims(prims, frame)
    sk, notes = _Sketch(), {}

    def refuse(*a):
        raise RuntimeError('3 : Some input argument is invalid')
    sk.sketchCurves.sketchArcs = type('A', (), {'addByThreePoints': lambda _, *a: refuse()})()
    n = ns['draw_prims_tidy'](sk, lifted, notes)
    assert notes['arcs_as_lines'] == 4 and notes['tangent_failed'] == 8 and 'tangent' not in notes
    assert n == 4 + 4 * 2 and all(c.kind == 'line' for c in sk.curves)


def test_xray_service_script_with_tidy(tmp_path):
    from stl_to_solid.service import xray
    p = synth.export(synth.rounded_rect(), str(tmp_path / 'rr.stl'))
    plain = xray.script(p, 'z', -1, 1, 1, [-1.0, 0.0, 1.0])
    tidy = xray.script(p, 'z', -1, 1, 1, [-1.0, 0.0, 1.0], tidy=True, sharpen=0.08)
    assert 'draw_prims_tidy' not in plain['text'] and 'draw_prims_tidy' in tidy['text']
    assert "'tan0': True" in tidy['text'] and "'tan0'" not in plain['text']


# --- API and MCP -----------------------------------------------------------

def test_section_api_takes_tidy_and_checks_sharpen(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv('STLTOSOLID_DATA', str(tmp_path / 'data'))
    from backend import jobs, main
    importlib.reload(jobs)
    p = synth.export(synth.rounded_rect(), tmp_path / 'rr.stl')
    with TestClient(main.app) as c:
        with open(p, 'rb') as fh:
            r = c.post('/api/jobs', files={'file': ('rr.stl', fh, 'application/octet-stream')})
        job = r.json()['id']
        r = c.get(f'/api/jobs/{job}/section', params={'axis': 'z', 'tidy': 'true', 'sharpen': 0.08})
        assert r.status_code == 200, r.text
        assert r.json()['stats']['tangent'] == 8
        r = c.get(f'/api/jobs/{job}/section', params={'axis': 'z'})
        assert r.json()['stats']['tangent'] == 0
        for route in ('section', 'section-script', 'xray-script'):
            r = c.get(f'/api/jobs/{job}/{route}', params={'axis': 'z', 'sharpen': 2})
            assert r.status_code == 400 and 'sharpen' in r.text, route
        r = c.get(f'/api/jobs/{job}/xray-script', params={'axis': 'z', 'from': -1, 'to': 1, 'step': 1,
                                                          'tidy': 'true'})
        assert r.status_code == 200 and 'draw_prims_tidy' in r.text


def test_mcp_tools_take_tidy(tmp_path):
    pytest.importorskip('mcp')
    pytest.importorskip('PIL')
    from mcp import Client
    from stl_to_solid_mcp import config as C
    from stl_to_solid_mcp.server import build_server
    cfg = C.from_args(out=str(tmp_path / 'out'))
    part = synth.export(synth.rounded_rect(), str(tmp_path / 'rr.stl'))

    async def go():
        async with Client(build_server(cfg)) as c:
            tr = await c.call_tool('trace_section', {'path': part, 'axis': 'z', 'tidy': True,
                                                     'sharpen': 0.08})
            xs = await c.call_tool('xray_fusion_script', {'path': part, 'axis': 'z', 'from_mm': -1,
                                                          'to_mm': 1, 'step_mm': 1, 'tidy': True})
            try:
                bad = await c.call_tool('trace_section', {'path': part, 'sharpen': 2})
                bad_err = bad.is_error
            except Exception:
                bad_err = True
            return tr, xs, bad_err
    tr, xs, bad_err = asyncio.run(go())
    assert tr.structured_content['stats']['tangent'] == 8
    with open(xs.structured_content['script']) as f:
        assert 'draw_prims_tidy' in f.read()
    assert bad_err
