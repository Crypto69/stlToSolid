"""Where the solid and the mesh disagree, as volumes: ranked "places".

The deviation numbers say how far the worst point is off; they do not say
whether that is a hair along a whole wall or a boss that was never built.
Here both are filled on one voxel grid and the cells where they disagree
are grouped into connected regions:

  * missing — inside the mesh, outside the solid (material the rebuild lacks)
  * excess  — inside the solid, outside the mesh (material it added: a hole
              not cut, a filled pocket)

each with its volume (mm^3), depth (how far its deepest cell lies from
the region's edge) and box, largest first. Cells next to the mesh surface
are left out: a surface off by less than a cell is the deviation gate's
business, not a place. (ScanRuler ranks its "places" the same way, from a
grid compared on the generalised winding number.)

Inside tests: a closed mesh is filled by flood fill from its surface
voxels (each closed shell filled on its own, then counted by parity, so a
cavity stays empty); a leaky mesh, which a flood fill would run through,
by the generalised winding number (Jacobson et al. 2013) of a decimated
copy, which holes do not break.
"""
import numpy as np

MAX_CELLS = 120_000        # grid size budget (about 1-2 s per body)
TOP = 5                    # places reported
WINDING_FACES = 4000       # a leaky mesh is decimated to this for the winding number


def _grid(bounds, max_cells):
    lo, hi = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    ext = np.maximum(hi - lo, 1e-6)
    h = float((np.prod(ext) / max_cells) ** (1 / 3))
    lo = lo - 2 * h
    shape = np.ceil((ext + 4 * h) / h).astype(int) + 1
    return lo, h, shape


def _surface_points(mesh, step):
    """Points on every triangle no further than `step` apart: a barycentric
    lattice per triangle, k divisions with k = longest edge / step."""
    T = np.asarray(mesh.triangles, float)
    if not len(T):
        return np.zeros((0, 3))
    L = np.max(np.linalg.norm(T - np.roll(T, 1, axis=1), axis=2), axis=1)
    K = np.clip(np.ceil(L / step).astype(int), 1, 400)
    out = []
    for k in np.unique(K):
        tri = T[K == k]
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing='ij')
        keep = (i + j) <= k
        w0, w1 = i[keep] / k, j[keep] / k
        W = np.column_stack([w0, w1, 1.0 - w0 - w1])          # (n_lat, 3)
        out.append(np.einsum('lc,tcd->tld', W, tri).reshape(-1, 3))
    return np.vstack(out)


def _surface_cells(mesh, lo, h, shape):
    """Boolean grid of the cells the surface passes through: points every
    half cell on every triangle touch every cell the surface crosses
    (random samples leave gaps a flood fill leaks through)."""
    pts = _surface_points(mesh, 0.5 * h)
    ix = np.floor((pts - lo) / h).astype(int)
    ix = np.clip(ix, 0, np.asarray(shape) - 1)
    g = np.zeros(shape, bool)
    g[ix[:, 0], ix[:, 1], ix[:, 2]] = True
    return g


def _fill_closed(mesh, lo, h, shape):
    """Inside cells of a closed mesh: each connected shell flood-filled on
    its own, then nested by parity (a cavity shell inside the outer one
    cancels it)."""
    from scipy import ndimage
    shells = mesh.split(only_watertight=False) if len(mesh.faces) else []
    count = np.zeros(shape, np.int16)
    for sh in shells:
        surf = _surface_cells(sh, lo, h, shape)
        count += ndimage.binary_fill_holes(surf)
    return (count % 2).astype(bool)


def winding_number(mesh, P, chunk=64):
    """Generalised winding number of points P with respect to `mesh`
    (1 inside, 0 outside, in between near holes): the solid angle of every
    triangle seen from each point (Van Oosterom & Strackee), summed."""
    T = np.asarray(mesh.triangles, float)
    out = np.empty(len(P))
    for s in range(0, len(P), chunk):
        p = P[s:s + chunk][:, None, None, :]
        a, b, c = T[None, :, 0] - p[:, :, 0], T[None, :, 1] - p[:, :, 0], T[None, :, 2] - p[:, :, 0]
        la, lb, lc = (np.linalg.norm(x, axis=-1) for x in (a, b, c))
        det = np.einsum('ijk,ijk->ij', a, np.cross(b, c))
        den = (la * lb * lc + np.einsum('ijk,ijk->ij', a, b) * lc
               + np.einsum('ijk,ijk->ij', b, c) * la + np.einsum('ijk,ijk->ij', c, a) * lb)
        out[s:s + chunk] = np.arctan2(det, den).sum(axis=1) / (2 * np.pi)
    return out


def _fill_leaky(mesh, lo, h, shape):
    """Inside cells of a mesh with holes, by winding number > 0.5 at the
    cell centres (on a decimated copy: the number is smooth and holes do
    not break it)."""
    m = mesh
    if len(m.faces) > WINDING_FACES:
        try:
            import fast_simplification
            import trimesh
            v, f = fast_simplification.simplify(np.asarray(m.vertices, np.float32),
                                                np.asarray(m.faces, np.int32),
                                                1.0 - WINDING_FACES / len(m.faces))
            m = trimesh.Trimesh(v, f, process=False)
        except Exception:
            pass
    idx = np.indices(shape).reshape(3, -1).T
    P = lo + (idx + 0.5) * h
    w = winding_number(m, P)
    return (np.abs(w) > 0.5).reshape(shape)


def solid_triangles(shape, deflection=0.1):
    """Triangle mesh of an OCC shape, read straight off its face
    triangulations (meshed at `deflection` if a face has none): far
    faster than a vertex-by-vertex tessellation on a solid of thousands
    of faces, and the validation pass has usually meshed it already."""
    import trimesh
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopAbs import TopAbs_REVERSED
    from .fillets import _faces
    shape = shape.wrapped if hasattr(shape, 'wrapped') else shape
    faces = _faces(shape)
    if any(BRep_Tool.Triangulation_s(f, TopLoc_Location()) is None for f in faces):
        BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, False)
    V, T, off = [], [], 0
    for f in faces:
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        n = tri.NbNodes()
        pts = np.empty((n, 3))
        for i in range(1, n + 1):
            p = tri.Node(i).Transformed(tr)
            pts[i - 1] = (p.X(), p.Y(), p.Z())
        m = tri.NbTriangles()
        tt = np.empty((m, 3), np.int64)
        for i in range(1, m + 1):
            tt[i - 1] = tri.Triangle(i).Get()
        tt -= 1
        if f.Orientation() == TopAbs_REVERSED:
            tt = tt[:, ::-1]
        V.append(pts)
        T.append(tt + off)
        off += n
    if not V:
        return trimesh.Trimesh()
    out = trimesh.Trimesh(np.vstack(V), np.vstack(T), process=True)
    out.merge_vertices()
    return out


def volume_places(solid_mesh, mesh, max_cells=MAX_CELLS, top=TOP):
    """Ranked places where `solid_mesh` (the rebuilt solid, tessellated)
    and `mesh` disagree. Returns {'cell_mm', 'missing_mm3', 'excess_mm3',
    'places': [{'kind', 'volume_mm3', 'depth_mm', 'center', 'box'}]}."""
    from scipy import ndimage
    bounds = np.vstack([np.minimum(mesh.bounds[0], solid_mesh.bounds[0]),
                        np.maximum(mesh.bounds[1], solid_mesh.bounds[1])])
    lo, h, shape = _grid(bounds, max_cells)
    fill = _fill_closed if mesh.is_watertight else _fill_leaky
    in_m = fill(mesh, lo, h, shape)
    in_s = _fill_closed(solid_mesh, lo, h, shape)
    # a cell the mesh surface passes through is the surface's business
    band = ndimage.binary_dilation(_surface_cells(mesh, lo, h, shape))
    out = {'cell_mm': h, 'places': []}
    cell = h ** 3
    for kind, diff in (('missing', in_m & ~in_s & ~band), ('excess', in_s & ~in_m & ~band)):
        out[f'{kind}_mm3'] = float(diff.sum() * cell)
        lab, n = ndimage.label(diff)
        if not n:
            continue
        sizes = ndimage.sum(diff, lab, index=np.arange(1, n + 1))
        dist = ndimage.distance_transform_edt(diff)
        for k in np.argsort(-sizes)[:top]:
            sel = lab == (k + 1)
            if sizes[k] < 2:
                continue
            ix = np.argwhere(sel)
            out['places'].append({
                'kind': kind,
                'volume_mm3': float(sizes[k] * cell),
                'depth_mm': float(dist[sel].max() * h),
                'center': (lo + (ix.mean(axis=0) + 0.5) * h).round(3).tolist(),
                'box': [(lo + ix.min(axis=0) * h).round(3).tolist(),
                        (lo + (ix.max(axis=0) + 1) * h).round(3).tolist()]})
    out['places'].sort(key=lambda p: -p['volume_mm3'])
    out['places'] = out['places'][:top]
    return out
