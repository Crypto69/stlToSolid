"""Half model (issue #2): find the X/Y/Z mirror plane of a part, write only
one side of it, and give every generated script the same cut."""
import ast
import math
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh
import cadquery as cq

from . import synth


def mirror_part(shift=7.5):
    """A 60x30x6 plate with D5 through holes at x = +-20 and a D8 boss 6
    tall at (0, 8): mirror-symmetric about X = shift only (the boss breaks Y
    and Z). Shifted so the plane is not the origin."""
    wp = (cq.Workplane('XY').box(60, 30, 6)
          .faces('>Z').workplane().pushPoints([(-20, 0), (20, 0)]).hole(5)
          .faces('>Z').workplane().center(0, 8).circle(4).extrude(6))
    return wp.translate((shift, 0, 0))


PLATE_VOL = 60 * 30 * 6 - 2 * math.pi * 2.5 ** 2 * 6 + math.pi * 4 ** 2 * 6


@pytest.fixture
def plate(tmp_path):
    return synth.export(mirror_part(), tmp_path / 'plate.stl')


# ---------- finding the plane ----------

def test_mirror_planes_find_the_one_symmetric_axis(plate):
    from stl_to_solid.half import mirror_planes, suggest
    pl = {p['axis']: p for p in mirror_planes(trimesh.load(plate, force='mesh'))}
    assert pl['x']['symmetric'] and pl['x']['match'] > 0.99
    assert pl['x']['plane_mm'] == pytest.approx(7.5, abs=0.05)
    # the D8 boss is about 4% of the surface: nearly symmetric about Y
    assert not pl['y']['symmetric'] and pl['y']['nearly']
    assert not pl['z']['symmetric'] and not pl['z']['nearly']
    assert suggest(list(pl.values())) == 'x'


def test_a_lumpy_mesh_still_reads_symmetric(plate, tmp_path):
    """AI-made and scanned meshes are never exact: per-vertex noise must not
    hide the symmetry, and the exact pass must not chase it."""
    from stl_to_solid.half import mirror_planes, resolve
    noisy = synth.jitter(plate, str(tmp_path / 'noisy.stl'), sigma=0.05, max_edge=1.0)
    m = trimesh.load(noisy, force='mesh')
    pl = {p['axis']: p for p in mirror_planes(m)}
    assert pl['x']['symmetric'] and not pl['y']['symmetric'] and not pl['z']['nearly']
    spec = resolve(m, 'x', verbose=False)
    assert spec['source'] == 'mirror' and spec['plane_mm'] == pytest.approx(7.5, abs=0.05)


def test_exact_pass_puts_a_cad_plane_on_float_precision(plate):
    from stl_to_solid.half import resolve
    spec = resolve(trimesh.load(plate, force='mesh'), 'x', verbose=False)
    assert spec['plane_mm'] == pytest.approx(7.5, abs=1e-6)


def test_not_symmetric_falls_back_to_the_centre_and_says_so(plate, capsys):
    from stl_to_solid.half import resolve
    m = trimesh.load(plate, force='mesh')
    spec = resolve(m, 'z', verbose=True)
    assert spec['source'] == 'centre' and spec['symmetric'] is False and spec['nearly'] is False
    assert spec['plane_mm'] == pytest.approx(m.bounding_box.centroid[2])
    assert 'NOT symmetric' in capsys.readouterr().out


def test_bad_axis_and_side_are_refused():
    from stl_to_solid.half import resolve, HalfError
    m = trimesh.creation.box((10, 10, 10))
    with pytest.raises(HalfError):
        resolve(m, 'w', verbose=False)
    with pytest.raises(HalfError):
        resolve(m, 'x', keep='left', verbose=False)


# ---------- the cut, through run() ----------

def _step_stats(path):
    s = cq.importers.importStep(str(path))
    solids = s.solids().vals()
    bb = cq.Compound.makeCompound(solids).BoundingBox()
    return {'solids': len(solids), 'volume': sum(v.Volume() for v in solids),
            'lo': (bb.xmin, bb.ymin, bb.zmin), 'hi': (bb.xmax, bb.ymax, bb.zmax),
            'valid': all(v.isValid() for v in solids)}


@pytest.mark.parametrize('keep', ['+', '-'])
def test_half_run_writes_one_side(plate, tmp_path, keep):
    from stl_to_solid.pipeline import run
    out = tmp_path / f'half{keep}.step'
    r = run(plate, str(out), verbose=False, half='x', half_keep=keep, workers=0)
    assert r['mode'] == 'prismatic'
    h = r['half']
    assert h['axis'] == 'x' and h['keep'] == keep and h['source'] == 'mirror' and h['symmetric']
    assert h['volume_kept_pct'] == pytest.approx(50.0, abs=0.1) and h['bodies_cut'] == 1
    st = _step_stats(out)
    assert st['solids'] == 1 and st['valid']
    assert st['volume'] == pytest.approx(PLATE_VOL / 2, rel=3e-3)
    if keep == '+':
        assert st['lo'][0] == pytest.approx(7.5, abs=1e-3) and st['hi'][0] == pytest.approx(37.5, abs=1e-2)
    else:
        assert st['hi'][0] == pytest.approx(7.5, abs=1e-3) and st['lo'][0] == pytest.approx(-22.5, abs=1e-2)


def test_half_scripts_make_the_same_half(plate, tmp_path):
    """The CadQuery script, run on its own, regenerates the half; the
    Fusion script ends with the cut inside run()'s try block."""
    from stl_to_solid.pipeline import run
    out = tmp_path / 'part.step'
    r = run(plate, str(out), verbose=False, half='x', workers=0)
    want = _step_stats(out)
    os.rename(out, tmp_path / 'from_run.step')
    res = subprocess.run([sys.executable, r['script']], cwd=str(tmp_path), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    got = _step_stats(tmp_path / 'part.step')
    assert got['solids'] == 1 and got['volume'] == pytest.approx(want['volume'], rel=2e-3)
    assert got['lo'][0] == pytest.approx(7.5, abs=1e-3)

    text = open(r['fusion_script'], encoding='utf-8').read()
    tree = ast.parse(text)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert '_half_cut' in fns
    tryb = next(n for n in fns['run'].body if isinstance(n, ast.Try))
    last = tryb.body[-1]
    assert isinstance(last, ast.Expr) and last.value.func.id == '_half_cut'
    assert [a.value for a in last.value.args[1:]] == [0, pytest.approx(0.75), 1]


def test_half_at_a_given_offset(plate, tmp_path):
    """10 mm right of the centre, keep +: the plate from x = 17.5 to 37.5
    with one hole in it and no boss."""
    from stl_to_solid.pipeline import run
    out = tmp_path / 'at.step'
    r = run(plate, str(out), verbose=False, half='x', half_at=10.0, workers=0)
    assert r['half']['source'] == 'given' and r['half']['plane_mm'] == pytest.approx(17.5)
    st = _step_stats(out)
    assert st['volume'] == pytest.approx(20 * 30 * 6 - math.pi * 2.5 ** 2 * 6, rel=3e-3)
    assert st['lo'][0] == pytest.approx(17.5, abs=1e-3)


def test_nothing_on_the_kept_side_is_an_error(plate, tmp_path):
    from stl_to_solid.pipeline import run
    from stl_to_solid.half import HalfError
    with pytest.raises(HalfError, match='no half'):
        run(plate, str(tmp_path / 'x.step'), verbose=False, half='x', half_at=100.0, workers=0)


def test_multi_body_skips_the_far_side_and_keeps_the_near_side_whole(tmp_path):
    """Three cubes along X (a toy car's wheels): the far one is not even
    converted, the middle one is cut, the near one is written whole."""
    from stl_to_solid.pipeline import run
    parts = []
    for x in (-40, 0, 40):
        b = trimesh.creation.box((10, 10, 10))
        b.apply_translation((x, 0, 0))
        parts.append(b)
    p = str(tmp_path / 'three.stl')
    trimesh.util.concatenate(parts).export(p)
    out = tmp_path / 'three.step'
    r = run(p, str(out), verbose=False, half='x', workers=0)
    h = r['half']
    assert h['source'] == 'mirror' and h['plane_mm'] == pytest.approx(0.0, abs=1e-6)
    assert (h['bodies_skipped'], h['bodies_cut'], h['bodies_kept_whole']) == (1, 1, 1)
    assert r['n_written'] == 2
    st = _step_stats(out)
    assert st['solids'] == 2 and st['volume'] == pytest.approx(1500.0, rel=1e-3)
    assert st['lo'][0] == pytest.approx(0.0, abs=1e-3)


def test_faceted_bodies_are_cut_too(tmp_path):
    """A sphere goes the faceted way; the cut does not care which route a
    body took."""
    from stl_to_solid.pipeline import run
    s = trimesh.creation.icosphere(subdivisions=3, radius=10)
    p = str(tmp_path / 'ball.stl')
    s.export(p)
    out = tmp_path / 'ball.step'
    r = run(p, str(out), verbose=False, half='z', workers=0, face_groups=False)
    assert r['mode'] == 'faceted' and r['half']['symmetric']
    st = _step_stats(out)
    assert st['solids'] == 1 and st['volume'] == pytest.approx(s.volume / 2, rel=5e-3)
    assert st['lo'][2] == pytest.approx(0.0, abs=1e-4)


def test_cli_half_flags(plate, tmp_path):
    out = tmp_path / 'cli.step'
    res = subprocess.run([sys.executable, '-m', 'stl_to_solid.pipeline', plate, str(out),
                          '--half', 'x', '--half-keep', '-', '--workers', '0'],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert '[half] keeping the -X side' in res.stdout
    assert _step_stats(out)['hi'][0] == pytest.approx(7.5, abs=1e-3)


# ---------- the Fusion step, against a fake Fusion ----------

class _P:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Body:
    def __init__(self, name, lo, hi):
        self.name, self.lo, self.hi = name, lo, hi
        self.isSolid = self.isVisible = True
        self.boundingBox = SimpleNamespace(minPoint=_P(*lo), maxPoint=_P(*hi))
        c = [(a + b) / 2 for a, b in zip(lo, hi)]
        self.physicalProperties = SimpleNamespace(centerOfMass=_P(*c))


class _Coll:
    def __init__(self, items):
        self.items = items

    @property
    def count(self):
        return len(self.items)

    def item(self, i):
        return self.items[i]


def _fake_root(bodies, flip=False, direct=False):
    """A root component whose offset planes land on the other side when
    `flip` (Fusion may measure an offset against the plane's normal) and whose
    split cuts a body's box in two at the plane."""
    log = []
    body_list = list(bodies)

    class Planes:
        def __init__(self):
            self.calls = 0

        def createInput(self):
            return SimpleNamespace(setByOffset=lambda base, off: setattr(self, 'off', off),
                                   setByPlane=lambda pl: setattr(self, 'off', pl))

        def add(self, inp):
            self.calls += 1
            off = self.off
            if direct:
                o = off.origin
            else:
                v = -off if flip else off
                o = [0.0, 0.0, 0.0]
                o[self.k] = v
                o = _P(*o)
            pl = SimpleNamespace(geometry=SimpleNamespace(origin=o), name=None,
                                 deleteMe=lambda: log.append(('plane-deleted',)))
            self.last = pl
            return pl

    planes = Planes()

    def split_input(body, plane, extend):
        return (body, plane)

    def split_add(inp):
        b, pl = inp
        o = pl.geometry.origin
        at = (o.x, o.y, o.z)[planes.k]
        lo, hi = list(b.lo), list(b.hi)
        a_hi, b_lo = list(hi), list(lo)
        a_hi[planes.k] = at
        b_lo[planes.k] = at
        body_list.remove(b)
        body_list.extend([_Body(b.name + '<', lo, a_hi), _Body(b.name + '>', b_lo, hi)])
        log.append(('split', b.name, at))

    def remove_add(b):
        body_list.remove(b)
        log.append(('removed', b.name))

    class Bodies:
        @property
        def count(self):
            return len(body_list)

        def item(self, i):
            return body_list[i]

    design = SimpleNamespace(designType='direct' if direct else 'parametric')
    root = SimpleNamespace(
        constructionPlanes=planes, parentDesign=design, bRepBodies=Bodies(),
        yZConstructionPlane='yz', xZConstructionPlane='xz', xYConstructionPlane='xy',
        features=SimpleNamespace(
            splitBodyFeatures=SimpleNamespace(createInput=split_input, add=split_add),
            removeFeatures=SimpleNamespace(add=remove_add)))
    return root, body_list, log, planes


def _helper_ns():
    from stl_to_solid.half import FUSION_HALF_HELPER
    adsk = SimpleNamespace(
        core=SimpleNamespace(ValueInput=SimpleNamespace(createByReal=lambda v: v),
                             Plane=SimpleNamespace(create=lambda o, n: SimpleNamespace(origin=o, normal=n)),
                             Point3D=SimpleNamespace(create=lambda *a: _P(*a)),
                             Vector3D=SimpleNamespace(create=lambda *a: a)),
        fusion=SimpleNamespace(DesignTypes=SimpleNamespace(DirectDesignType='direct')))
    ns = {'adsk': adsk}
    exec(FUSION_HALF_HELPER, ns)
    return ns


@pytest.mark.parametrize('flip,direct', [(False, False), (True, False), (False, True)])
def test_fusion_half_cut_splits_and_removes_the_far_side(flip, direct):
    ns = _helper_ns()
    bodies = [_Body('crossing', (-3, 0, 0), (3, 1, 1)), _Body('near', (1, 0, 0), (2, 1, 1)),
              _Body('far', (-2, 0, 0), (-1, 1, 1))]
    root, live, log, planes = _fake_root(bodies, flip=flip, direct=direct)
    planes.k = 0
    kept, removed = ns['_half_cut'](root, 0, 0.5, 1)
    assert ('split', 'crossing', 0.5) in log
    assert not any(e[0] == 'split' and e[1] != 'crossing' for e in log)
    assert sorted(b.name for b in live) == ['crossing>', 'near']
    assert (kept, removed) == (2, 2)
    assert (('plane-deleted',) in log) == flip


def test_fusion_half_cut_keeps_the_minus_side():
    ns = _helper_ns()
    root, live, log, planes = _fake_root([_Body('b', (0, -4, 0), (1, 4, 1))])
    planes.k = 1
    assert ns['_half_cut'](root, 1, 0.0, -1) == (1, 1)
    assert [b.name for b in live] == ['b<']


def test_add_fusion_half_refuses_a_script_without_run():
    from stl_to_solid.half import add_fusion_half, HalfError
    with pytest.raises(HalfError):
        add_fusion_half('print(1)\n', {'axis': 'x', 'index': 0, 'keep': '+', 'plane_mm': 0.0})


def test_boundary_fill_script_gets_the_cut(tmp_path):
    """The face-group route's Boundary Fill script: the cut is the last step
    of its run() too, and the file still compiles."""
    from stl_to_solid.pipeline import run
    p = synth.export(synth.sphere_boss(), tmp_path / 'sb.stl')
    r = run(p, str(tmp_path / 'sb.step'), verbose=False, half='x', workers=0)
    assert r['mode'] == 'facegroup'
    if not r.get('bfill_script'):
        pytest.skip(f"route {r['mode']} wrote no Boundary Fill script")
    text = open(r['bfill_script'], encoding='utf-8').read()
    tree = ast.parse(text)
    run_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
    tryb = next(n for n in run_fn.body if isinstance(n, ast.Try))
    assert tryb.body[-1].value.func.id == '_half_cut'


# ---------- the doors: upload stats, web API, MCP ----------

def test_upload_stats_carry_the_symmetry(plate):
    from stl_to_solid.service.analysis import mesh_stats
    sym = mesh_stats(plate)['symmetry']
    assert sym['suggested'] == 'x'
    x = next(p for p in sym['planes'] if p['axis'] == 'x')
    assert x['symmetric'] and abs(x['offset']) < 0.05


def test_inspect_mesh_reports_the_half_hint_in_converted_mm(plate):
    from stl_to_solid.service.calls import inspect_mesh
    got = inspect_mesh(plate, None, {'units': 'cm', 'scale': 1.0})['symmetry']
    assert got['suggested_half'] == 'x' and "half='x'" in got['why']
    assert all('offset_mm' in p and 'offset' not in p for p in got['planes'])


def test_web_api_takes_and_checks_the_half_fields():
    pytest.importorskip('fastapi')
    from pydantic import ValidationError
    from backend.main import ConvertParams
    p = ConvertParams(half='y', half_keep='-', half_at=2.5)
    assert (p.half, p.half_keep, p.half_at) == ('y', '-', 2.5)
    assert ConvertParams().half is None
    with pytest.raises(ValidationError):
        ConvertParams(half='w')
    with pytest.raises(ValidationError):
        ConvertParams(half='x', half_keep='left')


def test_worker_passes_the_half_fields_to_run(plate, tmp_path, monkeypatch):
    """The web app and the MCP server both convert through the worker: the
    half fields must reach run() from params.json."""
    import json
    from stl_to_solid.service import worker
    d = tmp_path / 'job'
    d.mkdir()
    src = d / 'input.stl'
    src.write_bytes(open(plate, 'rb').read())
    params = dict(tol=0.08, accept_p95=0.25, accept_max=0.26, accept_hole_max=0.10, accept_vol_pct=2.0,
                  force_prismatic=False, half='x', half_keep='-', half_at=None)
    (d / 'params.json').write_text(json.dumps(params))
    seen = {}

    def fake_run(in_path, out_path, **kw):
        seen.update(kw)
        raise RuntimeError('stop here')
    import stl_to_solid
    monkeypatch.setattr(stl_to_solid, 'run', fake_run, raising=False)
    with pytest.raises(SystemExit):        # a failed run exits non-zero, after result.json
        worker.main([str(src), str(d / 'out.step'), str(d / 'params.json'), str(d / 'result.json')])
    assert seen.get('half') == 'x' and seen.get('half_keep') == '-' and seen.get('half_at') is None
    assert 'stop here' in json.loads((d / 'result.json').read_text())['error']
