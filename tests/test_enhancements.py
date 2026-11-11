"""Ideas from the ScanRuler comparison (SCANRULER-RESEARCH.md): scan noise
sets the tolerance floor, Booleans are volume-checked, measured values snap
to nominals within tolerance, and an arc must beat a line by more than the
noise explains."""
import numpy as np
import pytest
import trimesh

from . import synth


# ---------- nominal snapping ----------

def test_nominal_takes_the_roundest_value_within_tolerance():
    from stl_to_solid.nominal import nominal
    assert nominal(2.9996, 1e-3) == (3.0, 1.0)
    assert nominal(2.98, 0.05) == (3.0, 1.0)          # coarsest grid first
    assert nominal(3.42, 0.05) == (3.4, 0.1)
    assert nominal(1.5875, 1e-3) == (1.5875, None)    # 1/16 in: never snapped across tolerance
    assert nominal(3.0012, 1e-3)[1] is None           # outside tolerance: kept


def test_diameter_snap_and_tolerance():
    from stl_to_solid.nominal import snap_diameter_radius, snap_radius, snap_tolerance, SNAP_FLOOR
    assert snap_diameter_radius(1.69998, 1e-3) == pytest.approx(1.7)   # Ø3.4
    assert snap_radius(0.4997, 1e-3) == pytest.approx(0.5)
    assert snap_tolerance(0.0, 0.08) == SNAP_FLOOR                    # CAD export: exact
    assert snap_tolerance(0.01, 0.08) == pytest.approx(0.03)          # scan: 3 sigma
    assert snap_tolerance(0.05, 0.08) == pytest.approx(0.08)          # never past the fit tol


def test_level_snap_keeps_slabs_touching_and_refuses_collapse():
    from stl_to_solid.rebuild import _level_map
    slabs = [{'z0': 0.0, 'z1': 2.9996}, {'z0': 2.9996, 'z1': 5.0003}]
    to = _level_map(slabs, 1e-3)
    assert to[2.9996] == pytest.approx(3.0)       # one height, one value: still touching
    assert to[5.0003] == pytest.approx(5.0)
    assert to[0.0] == 0.0
    # two heights that would land on one value: nothing moves
    thin = [{'z0': 0.0, 'z1': 0.4996}, {'z0': 0.4996, 'z1': 0.5004}]
    assert all(v == z for z, v in _level_map(thin, 1e-3).items())


# ---------- Boolean volume guard ----------

def test_guard_invariants():
    from stl_to_solid.boolguard import check
    assert check('cut', 100.0, 10.0, 90.0) is None
    assert check('cut', 100.0, 10.0, 0.0) is not None                # ate the body
    assert check('cut', 100.0, 10.0, 101.0) is not None              # gained volume
    assert check('fuse', 100.0, 10.0, 105.0) is None
    assert check('fuse', 100.0, 10.0, 60.0) is not None              # lost a piece
    assert check('fuse', 100.0, 10.0, 130.0) is not None


def test_guarded_cut_passes_good_cuts_and_refuses_bad_ones(monkeypatch):
    import cadquery as cq
    from stl_to_solid import boolguard
    box = cq.Workplane('XY').box(10, 10, 10)
    pin = cq.Workplane('XY').circle(1).extrude(20, both=True)
    r = boolguard.guarded_cut(box, pin, tol=1e-4)
    assert boolguard.volume(r) == pytest.approx(1000 - np.pi * 10, rel=1e-6)
    # a kernel that hands back an empty result for every try is refused
    monkeypatch.setattr(boolguard, '_run', lambda op, a, b, tol: cq.Workplane('XY'))
    with pytest.raises(boolguard.BooleanGuardError):
        boolguard.guarded_cut(box, pin, tol=1e-4)


# ---------- noise ----------

def test_mesh_noise_reads_a_scans_sigma(tmp_path):
    from stl_to_solid.mesh_prep import mesh_noise
    m = trimesh.load(synth.scan_like_cube(tmp_path / 'cube.stl', sigma=0.02), force='mesh')
    assert mesh_noise(m)['sigma'] == pytest.approx(0.02, rel=0.25)


def test_scan_noise_widens_the_tolerance(tmp_path, capsys):
    from stl_to_solid.pipeline import _convert_body, NOISE_K
    from stl_to_solid.mesh_prep import mesh_noise
    m = trimesh.load(synth.scan_like_cube(tmp_path / 'cube.stl', subdivisions=5, sigma=0.04), force='mesh')
    sigma = mesh_noise(m)['sigma']
    shape, mode, metrics = _convert_body(m, True, True, True, 0.08, 0.25, 0.26, 0.10, 2.0)
    assert metrics['noise_mm'] == pytest.approx(sigma)
    assert metrics['tol_used'] == pytest.approx(max(0.08, NOISE_K * sigma))
    assert '[noise] scan noise' in capsys.readouterr().out


# ---------- F-test: line or arc ----------

def _wobbly_run(amp=0.05, n=8, L=4.0):
    """A straight 4 mm run whose points bow by `amp` in a smile: exactly an
    arc (r = 40) as far as the points go."""
    x = np.linspace(0, L, n)
    y = amp * (1 - ((x - L / 2) / (L / 2)) ** 2)
    return np.column_stack([x, y])


def test_arc_must_beat_line_by_more_than_noise():
    from stl_to_solid.profile_fit import segment_polyline
    pts = _wobbly_run()
    exact = segment_polyline(pts, tol=0.08, closed=False)
    assert [p['type'] for p in exact] == ['arc']          # a CAD section: the bow is shape
    noisy = segment_polyline(pts, tol=0.08, closed=False, noise=0.03)
    assert [p['type'] for p in noisy] == ['line']         # on a 0.03 scan it is noise


def test_real_arc_survives_the_noise_test():
    from stl_to_solid.profile_fit import segment_polyline
    a = np.linspace(0, np.pi / 2, 40)
    pts = np.column_stack([5 * np.cos(a), 5 * np.sin(a)])
    prims = segment_polyline(pts, tol=0.08, closed=False, noise=0.03)
    assert [p['type'] for p in prims] == ['arc']
    assert prims[0]['r'] == pytest.approx(5.0, abs=1e-3)


# ---------- symmetry ----------

def test_mirror_planes_of_a_symmetric_plate(tmp_path):
    from stl_to_solid.symmetry import axis_mirror_plane
    from stl_to_solid.rebuild import _axis_basis
    m = trimesh.load(synth.export(synth.plate_holes(), tmp_path / 'p.stl'), force='mesh')
    planes = axis_mirror_plane(m, np.array([0, 0, 1.0]), _axis_basis(np.array([0, 0, 1.0])))
    normals = sorted(tuple(np.round(np.abs(p['normal']), 3)) for p in planes)
    assert normals == [(0.0, 1.0, 0.0), (1.0, 0.0, 0.0)]
    assert all(abs(p['d']) < 0.01 and p['rms'] < 0.01 for p in planes)


def test_symmetrise_pairs_twins_and_leaves_an_odd_hole():
    from stl_to_solid.symmetry import symmetrise_profiles
    def circ(x, y, r=2.5):
        return {'type': 'arc', 'full': True, 'center': np.array([x, y]), 'r': r}
    holes = [circ(-20.001, 5), circ(20.0, 5.0004), circ(0.3, -5)]   # twins + one 0.3 off the line
    outer = [{'type': 'line', 'p0': np.array([-30.0, -20]), 'p1': np.array([30.002, -20])},
             {'type': 'line', 'p0': np.array([30.002, -20]), 'p1': np.array([30.002, 20])},
             {'type': 'line', 'p0': np.array([30.002, 20]), 'p1': np.array([-30.0, 20])},
             {'type': 'line', 'p0': np.array([-30.0, 20]), 'p1': np.array([-30.0, -20])}]
    moved = symmetrise_profiles([(outer, holes)], np.array([1.0, 0]), 0.0, 0.01)
    assert holes[0]['center'][0] == pytest.approx(-holes[1]['center'][0])
    assert holes[0]['center'][1] == pytest.approx(holes[1]['center'][1])
    assert holes[2]['center'][0] == 0.3 and not holes[2].get('symmetric')   # not a twin: kept
    assert outer[1]['p0'][0] == pytest.approx(-outer[3]['p0'][0])            # the side walls
    assert moved >= 6


def test_symmetric_part_reports_its_planes(tmp_path):
    from stl_to_solid.pipeline import run
    r = run(synth.export(synth.plate_holes(), tmp_path / 'p.stl'), str(tmp_path / 'p.step'), verbose=False)
    sym = r['metrics'].get('symmetry')
    assert sym and len(sym) == 2 and all(s['pieces'] > 0 for s in sym)


# ---------- fillets ----------

@pytest.mark.parametrize('name', ['fillet_top', 'boss_fillet', 'filleted_hole', 'plate_holes_fillets'])
def test_rounds_come_off_and_go_back_on(name):
    from stl_to_solid import fillets as fl
    from stl_to_solid.pipeline import accurate_volume
    shp = getattr(synth, name)().val().wrapped
    blends = fl.blend_faces(shp)
    assert blends
    sharp, edges = fl.defeature(shp, blends)
    assert all(e is not None for e in edges)
    groups = {}
    for (_, _, r, _), pts in zip(blends, edges):
        groups.setdefault(round(r, 4), []).append(pts)
    back, mids = fl.apply_fillets(sharp, sorted(groups.items(), reverse=True))
    assert accurate_volume(back) == pytest.approx(accurate_volume(shp), rel=1e-6)


def test_rounded_part_becomes_extrude_plus_fillet(tmp_path):
    import subprocess
    import sys
    from stl_to_solid.pipeline import run, accurate_volume
    import cadquery as cq
    r = run(synth.export(synth.fillet_top(), tmp_path / 'f.stl'), str(tmp_path / 'f.step'), verbose=False)
    assert r['mode'] == 'prismatic'
    fusion = open(r['fusion_script']).read()
    assert '_fillet(root, ' in fusion and 'filletFeatures' in fusion
    # the CadQuery script rebuilds the same part, fillet included
    out = subprocess.run([sys.executable, r['script']], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = cq.importers.importStep(str(tmp_path / 'f.step'))
    assert accurate_volume(got) == pytest.approx(accurate_volume(synth.fillet_top()), rel=1e-4)


# ---------- curved strips ----------

def _strip_wall_part():
    """A block whose back wall is a curve written as 12 flat strips (the way
    an exporter facets a spline wall), with a ball on top so no single
    extrusion explains it (face-group route)."""
    import cadquery as cq
    x = np.linspace(40, 0, 13)
    y = 10 + 6 * np.sin(np.pi * (40 - x) / 40) + 4 * (40 - x) / 40
    prof = cq.Workplane('XY').polyline([(0, 0), (40, 0)] + list(zip(x, y))).close().extrude(12)
    return prof.union(cq.Workplane('XY').sphere(4).translate((20, 5, 12)))


def test_rows_of_flat_strips_become_one_curved_face(tmp_path):
    from stl_to_solid.pipeline import run
    p = synth.export(_strip_wall_part(), tmp_path / 's.stl', tol=0.02, ang=0.2)
    r = run(p, str(tmp_path / 's.step'), verbose=False)
    assert r['mode'] == 'facegroup'
    faces, kinds = synth.step_faces(str(tmp_path / 's.step'))
    assert faces < 12                      # 12 strips alone were 12 faces
    assert 'SURFACE_OF_LINEAR_EXTRUSION' in open(tmp_path / 's.step').read()
    assert r['metrics']['dev_max'] < 0.26


def test_strip_chain_rules():
    from stl_to_solid import curved_strips as cs
    assert cs.MIN_STRIPS >= 3 and cs.MAX_DEG <= 20


# ---------- volume places ----------

def _tess(wp):
    from stl_to_solid.pipeline import tessellate_solid
    m = tessellate_solid(wp)
    m = trimesh.Trimesh(m.vertices, m.faces)
    m.merge_vertices()
    return m


def test_places_find_uncut_holes_and_nothing_on_a_good_rebuild(tmp_path):
    import cadquery as cq
    from stl_to_solid.places import volume_places
    mesh = trimesh.load(synth.export(synth.plate_holes(), tmp_path / 'p.stl'), force='mesh')
    two_holes = (cq.Workplane('XY').box(60, 40, 6).faces('>Z').workplane()
                 .rect(40, 25, forConstruction=True).vertices('<X').hole(5))
    r = volume_places(_tess(two_holes), mesh)
    big = [p for p in r['places'] if p['volume_mm3'] > 20]
    assert len(big) == 2 and all(p['kind'] == 'excess' for p in big)
    assert sorted(round(p['center'][0]) for p in big) == [20, 20]
    assert volume_places(_tess(synth.plate_holes()), mesh)['places'] == []


def test_places_keep_cavities_empty(tmp_path):
    from stl_to_solid.places import volume_places
    hc = synth.hollow_cube()
    mesh = trimesh.load(synth.export(hc, tmp_path / 'h.stl'), force='mesh')
    assert volume_places(_tess(hc), mesh)['places'] == []


def test_winding_number_inside_and_outside():
    from stl_to_solid.places import winding_number
    box = trimesh.creation.box((2, 2, 2))
    w = winding_number(box, np.array([[0, 0, 0], [3, 0, 0]], float))
    assert w[0] == pytest.approx(1.0, abs=1e-6) and abs(w[1]) < 1e-6
