"""Half model: for a part that is mirror-symmetric about an X, Y or Z plane,
write only one side of it.

Finding the plane (`mirror_planes`): for each axis, reflect surface samples
through a plane square to it and find where each reflected point lands on
the surface (nearest neighbour in a dense surface sample: a KD-tree, about
a thousand times faster than trimesh's exact closest point on long thin
triangles, and good to a fraction of the 1 % score band). The plane's
offset is re-read from the pairs (the median of their midpoints, weighted
to samples whose surface faces along the axis: a point on a side wall
mirrors onto the same wall wherever the plane is) until it settles. The
score is the share of reflected samples that land within 1 % of the part's
diagonal of the surface. Mirror symmetry is a whole-surface property, so
this works on dense, lumpy AI-made or scanned meshes as well as on CAD
exports; symmetry.py is the exact, CAD-tolerance sibling used for
sketches.

Cutting (`cut_shape`): the finished solid is cut, not the mesh. Cutting the
mesh and capping the hole was tried first and left open halves on real
files (the flat cap's triangulation does not always meet the cut outline),
while OpenCascade's common with a box that covers the kept side gave one
valid solid on every test part. It also means every route (prismatic,
face-group, faceted, loft) is converted exactly as without the option.

The generated scripts rebuild the whole part, so each gets a cut step of
its own (`cadquery_half_tail`, `add_fusion_half`).
"""
import numpy as np

AXES = 'xyz'
SAMPLE = 3000           # surface points reflected
TREE_SAMPLE = 60000     # surface points the reflections are looked up in
TREE_VERTS = 200000     # vertices added to them (subsampled above this)
ITERS = 12
FACING_MIN = 0.3        # |normal . axis| for a sample to vote on the offset
MATCH_BAND = 0.01       # of the diagonal: a reflected point this close lands on the surface
# a part reads as symmetric about a plane when this share of its mirrored
# surface lands on the surface and the worst tenth stays inside the band;
# as nearly symmetric from NEAR_MATCH (a toy car's exhaust on one side, a
# controller's buttons): the plane is still the one to cut on, but the half
# keeps only one side's version of what differs
SYM_MATCH = 0.97
NEAR_MATCH = 0.90
SYM_P90 = 0.01


class HalfError(RuntimeError):
    """The half cut could not be made (nothing on the kept side, a failed
    boolean)."""


def _axis_index(axis):
    a = str(axis).strip().lower()
    if a not in AXES:
        raise HalfError(f"half axis must be x, y or z (got {axis!r})")
    return AXES.index(a)


def _keep_sign(keep):
    k = str(keep).strip()
    if k not in ('+', '-'):
        raise HalfError(f"half keep must be '+' or '-' (got {keep!r})")
    return 1.0 if k == '+' else -1.0


def _lookup(mesh, seed=0):
    """(samples, sample normals, KD-tree over the surface, diagonal)."""
    import trimesh
    from scipy.spatial import cKDTree
    P, fi = trimesh.sample.sample_surface(mesh, SAMPLE, seed=seed)
    N = np.asarray(mesh.face_normals[fi], float)
    Q, _ = trimesh.sample.sample_surface(mesh, TREE_SAMPLE, seed=seed + 1)
    V = np.asarray(mesh.vertices, float)
    if len(V) > TREE_VERTS:
        V = V[np.linspace(0, len(V) - 1, TREE_VERTS).astype(np.int64)]
    tree = cKDTree(np.vstack([np.asarray(Q, float), V]))
    diag = float(np.linalg.norm(mesh.extents))
    return np.asarray(P, float), N, tree, diag


def _score_axis(P, N, tree, diag, k, d0):
    """Settle the offset of the plane square to axis k from d0 and score it."""
    pts = tree.data
    w = np.abs(N[:, k])
    facing = w >= FACING_MIN
    d = float(d0)
    band = MATCH_BAND * diag
    for _ in range(ITERS):
        if facing.sum() < 3:
            break
        R = P[facing].copy()
        R[:, k] = 2.0 * d - R[:, k]
        dist, j = tree.query(R)
        keep = dist <= max(3.0 * float(np.median(dist)), band)
        if keep.sum() < 3:
            break
        mid = 0.5 * (P[facing][keep, k] + pts[j[keep], k])
        d_new = float(np.median(mid))
        if abs(d_new - d) < 1e-6 * diag:
            d = d_new
            break
        d = d_new
    R = P.copy()
    R[:, k] = 2.0 * d - R[:, k]
    dist, _ = tree.query(R)
    rel = dist / diag if diag > 0 else dist
    match = float(np.mean(rel <= MATCH_BAND))
    p90 = float(np.quantile(rel, 0.9))
    return d, match, p90


def mirror_planes(mesh, exact=True):
    """One entry per axis, x then y then z: {'axis', 'plane_mm' (the
    plane's coordinate along the axis), 'offset_mm' (from the bounding-box
    centre), 'match' (share of the mirrored surface that lands on the
    surface), 'p90_pct' (90th percentile miss, % of the diagonal),
    'symmetric', 'nearly' (symmetric but for a few per cent of the
    surface)}. With `exact`, the plane of every symmetric or nearly
    symmetric axis is re-read by exact_offset, so what the upload shows is
    where the conversion cuts. Empty for a mesh with no surface."""
    if mesh is None or len(mesh.faces) == 0 or float(mesh.area) <= 0:
        return []
    P, N, tree, diag = _lookup(mesh)
    c = np.asarray(mesh.bounding_box.centroid, float)
    out = []
    for k in range(3):
        d, match, p90 = _score_axis(P, N, tree, diag, k, c[k])
        tight = p90 <= SYM_P90
        sym = bool(tight and match >= SYM_MATCH)
        near = bool(tight and not sym and match >= NEAR_MATCH)
        if exact and (sym or near):
            try:
                d = exact_offset(mesh, k, d)
            except Exception:
                pass
        out.append({'axis': AXES[k], 'plane_mm': d, 'offset_mm': d - float(c[k]),
                    'match': match, 'p90_pct': 100.0 * p90, 'symmetric': sym,
                    'nearly': near})
    return out


EXACT_PTS = 300         # samples the exact pass looks up (trimesh closest point is slow)
EXACT_FACING = 0.9      # only surfaces square to the axis: their mirror image pins the plane


def exact_offset(mesh, k, d, iters=2):
    """The plane's offset re-read with exact closest points on the surfaces
    square to axis k. On a CAD export a face at a mirrors onto the face at
    2t - a, so one pass gives the plane t to float precision where the
    KD-tree's sample spacing leaves a hair; on a lumpy mesh it changes
    little. Returns d unchanged when too few samples face along the axis."""
    import trimesh
    from trimesh.proximity import closest_point
    P, fi = trimesh.sample.sample_surface(mesh, 4 * SAMPLE, seed=7)
    N = np.asarray(mesh.face_normals[fi], float)
    sel = np.abs(N[:, k]) >= EXACT_FACING
    P = np.asarray(P, float)[sel]
    if len(P) < 8:
        return d
    if len(P) > EXACT_PTS:
        P = P[np.linspace(0, len(P) - 1, EXACT_PTS).astype(np.int64)]
    diag = float(np.linalg.norm(mesh.extents))
    for _ in range(iters):
        R = P.copy()
        R[:, k] = 2.0 * d - R[:, k]
        Q, dist, _ = closest_point(mesh, R)
        keep = dist <= max(3.0 * float(np.median(dist)), MATCH_BAND * diag)
        if keep.sum() < 8:
            break
        d = float(np.median(0.5 * (P[keep, k] + Q[keep, k])))
    return d


def suggest(planes):
    """The axis to offer for a half model: the best-matching symmetric
    plane (a nearly symmetric one when none is), or None."""
    sym = [p for p in planes if p['symmetric'] or p.get('nearly')]
    if not sym:
        return None
    return max(sym, key=lambda p: (p['symmetric'], p['match'], -p['p90_pct']))['axis']


def resolve(mesh, axis, keep='+', at=None, verbose=True):
    """Where the cut goes, as a spec for cut_shape and the scripts:
    {'axis', 'index', 'keep', 'plane_mm', 'offset_mm', 'source', 'match',
    'p90_pct', 'symmetric'}. `mesh` is the whole part in converted mm.
    `at` None puts the plane where the part is mirror-symmetric about that
    axis (the box centre when it is not); a number is mm from the
    bounding-box centre."""
    k = _axis_index(axis)
    sign = _keep_sign(keep)
    c = float(mesh.bounding_box.centroid[k])
    found = None
    try:
        found = mirror_planes(mesh)[k]
    except Exception as e:
        if verbose:
            print(f"[half] mirror search failed ({type(e).__name__}: {e})")
    spec = {'axis': AXES[k], 'index': k, 'keep': '+' if sign > 0 else '-',
            'match': None if found is None else found['match'],
            'p90_pct': None if found is None else found['p90_pct'],
            'symmetric': None if found is None else found['symmetric'],
            'nearly': None if found is None else found['nearly']}
    if at is not None:
        spec.update(plane_mm=c + float(at), offset_mm=float(at), source='given')
    elif found is not None and (found['symmetric'] or found['nearly']):
        spec.update(plane_mm=found['plane_mm'], offset_mm=found['offset_mm'], source='mirror')
    else:
        spec.update(plane_mm=c, offset_mm=0.0, source='centre')
    if verbose:
        A = spec['axis'].upper()
        how = {'given': 'at the offset given',
               'mirror': 'on the mirror plane found',
               'centre': 'at the box centre'}[spec['source']]
        print(f"[half] keeping the {spec['keep']}{A} side of {A} = {spec['plane_mm']:.3f} mm "
              f"({spec['offset_mm']:+.3f} from the box centre, {how})")
        if found is not None:
            verdict = ('symmetric' if found['symmetric'] else
                       'nearly symmetric' if found['nearly'] else 'NOT symmetric')
            print(f"[half] mirror check about {A}: {100 * found['match']:.0f}% of the mirrored "
                  f"surface lands on the surface (90% within {found['p90_pct']:.2f}% of the "
                  f"diagonal) -> {verdict}")
            if found['nearly']:
                print(f"[half] note: about {100 * (1 - found['match']):.0f}% of the surface differs "
                      f"from its mirror image; the half keeps only this side's version of it")
            elif not found['symmetric']:
                print(f"[half] warning: the part does not read as mirror-symmetric about {A}; "
                      f"mirroring the half back will not give the original")
    return spec


def _bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    return np.array(b.CornerMin().Coord()), np.array(b.CornerMax().Coord())


def _volume(shape):
    from OCP.GProp import GProp_GProps
    from OCP.BRepGProp import BRepGProp
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return float(g.Mass())


def _n_solids(shape):
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    ex, n = TopExp_Explorer(shape, TopAbs_SOLID), 0
    while ex.More():
        n += 1
        ex.Next()
    return n


def _common(shape, box, fuzzy):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepCheck import BRepCheck_Analyzer
    op = BRepAlgoAPI_Common(shape, box) if not fuzzy else None
    if op is None:
        from OCP.TopTools import TopTools_ListOfShape
        op = BRepAlgoAPI_Common()
        a, b = TopTools_ListOfShape(), TopTools_ListOfShape()
        a.Append(shape)
        b.Append(box)
        op.SetArguments(a)
        op.SetTools(b)
        op.SetFuzzyValue(fuzzy)
        op.Build()
    if not op.IsDone():
        return None
    r = op.Shape()
    if r is None or r.IsNull() or _n_solids(r) == 0 or not BRepCheck_Analyzer(r).IsValid():
        return None
    return r


def cut_shape(shape, spec, tag='[half]', verbose=True):
    """`shape` cut back to the kept side of the plane. Returns (shape,
    info); shape is None when the whole body lies on the other side.
    Raises HalfError when the cut fails."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    k, sign, d = spec['index'], 1.0 if spec['keep'] == '+' else -1.0, float(spec['plane_mm'])
    lo, hi = _bbox(shape)
    span = float(np.max(hi - lo))
    eps = 1e-6 * max(span, 1.0)
    v0 = _volume(shape)
    info = {'volume_before_mm3': v0}
    # wholly on one side: no boolean
    if (sign > 0 and lo[k] >= d - eps) or (sign < 0 and hi[k] <= d + eps):
        info.update(state='kept', volume_after_mm3=v0)
        return shape, info
    if (sign > 0 and hi[k] <= d + eps) or (sign < 0 and lo[k] >= d - eps):
        info.update(state='dropped', volume_after_mm3=0.0)
        if verbose:
            print(f"{tag} the whole body lies on the cut-away side: left out")
        return None, info
    pad = 0.1 * span + 1.0
    blo, bhi = lo - pad, hi + pad
    if sign > 0:
        blo[k] = d
    else:
        bhi[k] = d
    box = BRepPrimAPI_MakeBox(gp_Pnt(*blo), gp_Pnt(*bhi)).Shape()
    r = None
    for fuzzy in (0.0, 1e-5, 1e-4):
        r = _common(shape, box, fuzzy)
        if r is not None:
            break
    if r is None:
        raise HalfError('the half cut failed: OpenCascade could not cut this body at '
                        f"{spec['axis'].upper()} = {d:.3f} mm")
    v1 = _volume(r)
    info.update(state='cut', volume_after_mm3=v1, solids=_n_solids(r))
    if verbose:
        print(f"{tag} cut at {spec['axis'].upper()} = {d:.3f} mm: volume {v0:.0f} -> {v1:.0f} mm^3 "
              f"({100 * v1 / v0 if v0 > 0 else 0:.1f}%)")
    return r, info


def summary(spec, infos):
    """The run's 'half' record: the spec plus what happened to the bodies."""
    out = {k: spec[k] for k in ('axis', 'keep', 'plane_mm', 'offset_mm', 'source',
                                'match', 'p90_pct', 'symmetric', 'nearly')}
    out['bodies_cut'] = sum(1 for i in infos if i['state'] == 'cut')
    out['bodies_kept_whole'] = sum(1 for i in infos if i['state'] == 'kept')
    out['bodies_dropped'] = sum(1 for i in infos if i['state'] == 'dropped')
    v0 = sum(i['volume_before_mm3'] for i in infos)
    v1 = sum(i['volume_after_mm3'] for i in infos)
    out['volume_before_mm3'] = v0
    out['volume_after_mm3'] = v1
    out['volume_kept_pct'] = 100.0 * v1 / v0 if v0 > 0 else None
    return out


# ---------- the generated scripts ----------

def cadquery_half_tail(spec, out_step_name):
    """Lines that cut the CadQuery script's `result` back to the kept side
    before it is exported."""
    k, d = spec['index'], float(spec['plane_mm'])
    keep = spec['keep']
    return [
        f"# ---- half model: keep the {keep}{spec['axis'].upper()} side of "
        f"{spec['axis'].upper()} = {d:.6f} mm (mirror it back in CAD) ----",
        f"HALF_AXIS, HALF_AT, HALF_KEEP = {k}, {d!r}, {keep!r}",
        'whole = cq.Compound.makeCompound([v for v in result.vals() if isinstance(v, cq.Shape)])',
        'bb = whole.BoundingBox()',
        'lo = [bb.xmin, bb.ymin, bb.zmin]',
        'hi = [bb.xmax, bb.ymax, bb.zmax]',
        'pad = 0.1 * max(h - l for l, h in zip(lo, hi)) + 1.0',
        'lo = [v - pad for v in lo]',
        'hi = [v + pad for v in hi]',
        "if HALF_KEEP == '+':",
        '    lo[HALF_AXIS] = HALF_AT',
        'else:',
        '    hi[HALF_AXIS] = HALF_AT',
        'keep_box = cq.Solid.makeBox(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], cq.Vector(*lo))',
        'result = cq.Workplane().add(whole.intersect(keep_box))',
    ]


FUSION_HALF_HELPER = '''

def _half_cut(root, k, at_cm, keep):
    """Half model: split every visible solid body at the plane square to
    axis k (0 X, 1 Y, 2 Z) at at_cm, then remove the bodies on the side not
    kept (keep +1 keeps the positive side). Returns (kept, removed)."""
    planes = root.constructionPlanes
    design = root.parentDesign
    normal = [0.0, 0.0, 0.0]
    normal[k] = 1.0
    origin = [0.0, 0.0, 0.0]
    origin[k] = at_cm
    pl = None
    if design.designType == adsk.fusion.DesignTypes.DirectDesignType:
        inp = planes.createInput()
        inp.setByPlane(adsk.core.Plane.create(adsk.core.Point3D.create(*origin),
                                              adsk.core.Vector3D.create(*normal)))
        pl = planes.add(inp)
    else:
        base = (root.yZConstructionPlane, root.xZConstructionPlane, root.xYConstructionPlane)[k]
        for off in (at_cm, -at_cm):
            inp = planes.createInput()
            inp.setByOffset(base, adsk.core.ValueInput.createByReal(off))
            p = planes.add(inp)
            o = p.geometry.origin
            if abs((o.x, o.y, o.z)[k] - at_cm) < 1e-5:
                pl = p
                break
            p.deleteMe()
    if pl is None:
        raise RuntimeError('half model: could not place the mirror plane')
    try:
        pl.name = 'stlToSolid mirror plane'
    except Exception:
        pass

    def solids():
        return [b for b in (root.bRepBodies.item(i) for i in range(root.bRepBodies.count))
                if b.isSolid and b.isVisible]

    splits = root.features.splitBodyFeatures
    for b in solids():
        bb = b.boundingBox
        lo = (bb.minPoint.x, bb.minPoint.y, bb.minPoint.z)[k]
        hi = (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z)[k]
        if lo < at_cm - 1e-6 and hi > at_cm + 1e-6:
            splits.add(splits.createInput(b, pl, True))
    kept, removed = 0, 0
    for b in solids():
        c = b.physicalProperties.centerOfMass
        if keep * ((c.x, c.y, c.z)[k] - at_cm) < 0:
            try:
                root.features.removeFeatures.add(b)
            except Exception:
                b.deleteMe()
            removed += 1
        else:
            kept += 1
    return kept, removed
'''


def add_fusion_half(text, spec):
    """A generated Fusion script with the half cut added as its last step
    (the helper before run(), the call at the end of run()'s try block).
    Raises HalfError when the script does not have the expected shape, so
    no script claims a half it does not make."""
    run_at = text.find('\ndef run(context):')
    exc_at = text.rfind('\n    except:')
    if run_at < 0 or exc_at < run_at:
        raise HalfError('the Fusion script has no run(context) try block to add the half cut to')
    keep = 1 if spec['keep'] == '+' else -1
    call = (f"\n        # ---- half model: keep the {spec['keep']}{spec['axis'].upper()} side of "
            f"{spec['axis'].upper()} = {spec['plane_mm']:.6f} mm (mirror it back with Mirror) ----"
            f"\n        _half_cut(root, {spec['index']}, {float(spec['plane_mm']) / 10.0!r}, {keep})")
    text = text[:exc_at] + call + text[exc_at:]
    return text[:run_at] + FUSION_HALF_HELPER + text[run_at:]
