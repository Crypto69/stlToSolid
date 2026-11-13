"""Curved walls the tessellation cut into flat strips, put back as one face.

A CAD exporter writes a gently curved wall that is not a cylinder (a
spline wall, an ellipse, a blend of varying radius) as a row of long flat
facets. The face-group engine fits each row as its own plane, so the solid
gets ten flat strips meeting at 1-2 degrees where the part has one smooth
face — the "ten facets that should be one curved wall" problem, and the
reason a gently curved plate never closes in Fusion's Boundary Fill.

Here such rows become one face of a new kind, 'extrusion': a planar curve
swept along a direction (Geom_SurfaceOfLinearExtrusion). The strips must

  * be planes meeting one after another at small angles (MIN..MAX_DEG),
    in a simple chain of at least MIN_STRIPS;
  * all lie square to one direction d (the smallest eigenvector of their
    area-weighted normal tensor, within SQUARE_DEG): an extruded profile;
  * be explained by the curve: the profile is interpolated through the
    strips' boundary lines (where the mesh vertices sit, on the true
    surface), and every vertex must lie on it within the vertex tolerance,
    every facet interior within the fit tolerance.

ScanRuler's `scan_segment` flags the same thing on scans ("planes" whose
sigma sits near the limit, joined smoothly at a few degrees, are strips of
one curved surface) and joins them `along` a direction.
"""
import numpy as np

MIN_STRIPS = 3
MIN_DEG = 0.3          # flatter than this the engine has already merged them
MAX_DEG = 15.0         # sharper is a real edge (a chamfer, a facet)
SQUARE_DEG = 0.5       # every strip normal this close to square to d
SAMPLES = 4000         # profile polyline used for closest-point projection


def _basis(d):
    a = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    b0 = np.cross(d, a)
    b0 /= np.linalg.norm(b0)
    return b0, np.cross(d, b0)


def _chains(regions, adj):
    """Simple chains of plane regions linked by small-angle neighbours."""
    planes = {r.id: r for r in regions if r.kind == 'plane'}
    link = {i: [] for i in planes}
    cos_lo, cos_hi = np.cos(np.radians(MAX_DEG)), np.cos(np.radians(MIN_DEG))
    for i, r in planes.items():
        for j in adj.get(i, ()):
            if j in planes and j != i:
                c = abs(float(np.dot(r.params['normal'], planes[j].params['normal'])))
                if cos_lo <= c <= cos_hi:
                    link[i].append(j)
    seen, out = set(), []
    for i in planes:
        if i in seen or not link[i]:
            continue
        comp, stack = [], [i]
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            comp.append(k)
            stack.extend(link[k])
        if len(comp) < MIN_STRIPS or any(len(link[k]) > 2 for k in comp):
            continue
        ends = [k for k in comp if len(link[k]) == 1]
        if len(ends) != 2:
            continue                       # a ring (closed loop): not handled
        order, prev, k = [ends[0]], None, ends[0]
        while True:
            nxt = [x for x in link[k] if x != prev]
            if not nxt:
                break
            prev, k = k, nxt[0]
            order.append(k)
        if len(order) == len(comp):
            out.append([planes[k] for k in order])
    return out


def _profile(mesh, strips):
    """(d, origin, b0, b1, 2-D profile points in chain order) or None."""
    F = mesh.faces
    V = np.asarray(mesh.vertices, float)
    N = np.array([s.params['normal'] for s in strips])
    A = np.array([s.area for s in strips])
    T = (N.T * A) @ N
    w, vec = np.linalg.eigh(T)
    d = vec[:, 0]
    if np.max(np.abs(N @ d)) > np.sin(np.radians(SQUARE_DEG)):
        return None
    vsets = [np.unique(F[s.faces]) for s in strips]
    allv = np.unique(np.concatenate(vsets))
    o = V[allv].mean(axis=0)
    b0, b1 = _basis(d)

    def uv(ix):
        P = V[ix] - o
        return np.column_stack([P @ b0, P @ b1])
    joints = []
    for a, b in zip(vsets[:-1], vsets[1:]):
        common = np.intersect1d(a, b)
        if len(common) == 0:
            return None
        joints.append(uv(common).mean(axis=0))
    # outer ends: the first / last strip's vertex farthest from its joint
    q0 = uv(vsets[0])
    q1 = uv(vsets[-1])
    e0 = q0[np.argmax(np.linalg.norm(q0 - joints[0], axis=1))]
    e1 = q1[np.argmax(np.linalg.norm(q1 - joints[-1], axis=1))]
    P = np.vstack([e0] + joints + [e1])
    if np.min(np.linalg.norm(np.diff(P, axis=0), axis=1)) < 1e-6:
        return None
    return d, o, b0, b1, P


def _curve3d(o, b0, b1, P2):
    """Cubic interpolating B-spline through the profile points, run on past
    both ends by a tenth of the end spans (the face is trimmed inside it)."""
    from OCP.GeomAPI import GeomAPI_Interpolate
    from OCP.TColgp import TColgp_HArray1OfPnt
    from OCP.gp import gp_Pnt
    ext0 = P2[0] + 0.1 * (P2[0] - P2[1])
    ext1 = P2[-1] + 0.1 * (P2[-1] - P2[-2])
    Q = np.vstack([ext0, P2, ext1])
    arr = TColgp_HArray1OfPnt(1, len(Q))
    for k, (u, v) in enumerate(Q, 1):
        p = o + u * b0 + v * b1
        arr.SetValue(k, gp_Pnt(*map(float, p)))
    it = GeomAPI_Interpolate(arr, False, 1e-9)
    it.Perform()
    if not it.IsDone():
        return None
    return it.Curve()


def _samples2d(curve, o, b0, b1, n=SAMPLES):
    t0, t1 = curve.FirstParameter(), curve.LastParameter()
    out = []
    for t in np.linspace(t0, t1, n):
        p = curve.Value(float(t))
        q = np.array([p.X(), p.Y(), p.Z()]) - o
        out.append((q @ b0, q @ b1))
    return np.array(out)


def _closest2d_many(pr, Q):
    """Closest points on the profile polyline for 2-D points Q: the nearest
    sample (KD-tree, built once per surface), then the exact foot on the
    two segments either side of it."""
    from scipy.spatial import cKDTree
    S = pr['samples']
    tree = pr.get('_tree')
    if tree is None:
        tree = pr['_tree'] = cKDTree(S)
    _, k = tree.query(Q)
    best = np.full((len(Q), 2), np.nan)
    bd = np.full(len(Q), np.inf)
    for off in (-1, 0):
        i = np.clip(k + off, 0, len(S) - 2)
        A, B = S[i], S[i + 1]
        AB = B - A
        L2 = np.maximum((AB * AB).sum(axis=1), 1e-30)
        t = np.clip(((Q - A) * AB).sum(axis=1) / L2, 0.0, 1.0)
        C = A + t[:, None] * AB
        d = ((C - Q) ** 2).sum(axis=1)
        sel = d < bd
        best[sel], bd[sel] = C[sel], d[sel]
    return best, np.clip(k, 0, len(S) - 2)


def _uvh(P, pr):
    rel = np.atleast_2d(np.asarray(P, float)) - pr['origin']
    return np.column_stack([rel @ pr['b0'], rel @ pr['b1']]), rel @ pr['dir']


def project_many(P, pr):
    Q, h = _uvh(P, pr)
    C, _ = _closest2d_many(pr, Q)
    return pr['origin'] + np.outer(C[:, 0], pr['b0']) + np.outer(C[:, 1], pr['b1']) + np.outer(h, pr['dir'])


def project(p, pr):
    """Closest point on an 'extrusion' surface (params pr) for one point."""
    return project_many(p, pr)[0]


def distance(P, pr):
    """Unsigned distance of points P to the surface."""
    P = np.atleast_2d(np.asarray(P, float))
    return np.linalg.norm(P - project_many(P, pr), axis=1)


def normals(P, pr):
    """Unit surface normals at the closest points of P (sign: the 2-D
    profile's left-hand normal; callers compare absolute values or try
    both orientations)."""
    S = pr['samples']
    Q, _ = _uvh(P, pr)
    _, k = _closest2d_many(pr, Q)
    t = S[k + 1] - S[k]
    t /= np.maximum(np.linalg.norm(t, axis=1), 1e-12)[:, None]
    return np.outer(-t[:, 1], pr['b0']) + np.outer(t[:, 0], pr['b1'])


def make_surface(pr):
    from OCP.Geom import Geom_SurfaceOfLinearExtrusion
    from OCP.gp import gp_Dir
    return Geom_SurfaceOfLinearExtrusion(pr['curve'], gp_Dir(*map(float, pr['dir'])))


def merge_strips(mesh, regions, adj, fit_tol, merge_tol, region_cls):
    """New region list with each qualifying strip chain replaced by one
    'extrusion' region (ids kept unique), and the list of merges as
    (new region, [the strips it replaced])."""
    F = mesh.faces
    V = np.asarray(mesh.vertices, float)
    C = np.asarray(mesh.triangles_center, float)
    next_id = max(r.id for r in regions) + 1
    gone, merges = set(), []
    for strips in _chains(regions, adj):
        prof = _profile(mesh, strips)
        if prof is None:
            continue
        d, o, b0, b1, P2 = prof
        curve = _curve3d(o, b0, b1, P2)
        if curve is None:
            continue
        pr = {'dir': d, 'origin': o, 'b0': b0, 'b1': b1, 'curve': curve,
              'samples': _samples2d(curve, o, b0, b1), 'profile': P2}
        faces = np.concatenate([s.faces for s in strips])
        vix = np.unique(F[faces])
        dv = distance(V[vix], pr)
        dc = distance(C[faces], pr)
        if dv.max() > max(4 * merge_tol, 1e-3) or dc.max() > fit_tol:
            continue
        rg = region_cls(next_id, 'extrusion', faces, [], pr,
                        resid=float(max(dv.max(), dc.max())), resid_v=float(dv.max()),
                        area=float(sum(s.area for s in strips)),
                        src=[i for s in strips for i in (s.src or [s.id])])
        next_id += 1
        merges.append((rg, strips))
        gone.update(s.id for s in strips)
    out = [r for r in regions if r.id not in gone] + [m[0] for m in merges]
    return out, merges
