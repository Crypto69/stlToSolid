"""Mirror symmetry: find the plane a part is symmetric about, and make the
fitted sketch exactly symmetric about it.

Finding the plane (`mirror_plane`): sample the surface, reflect the sample
through a candidate plane, find where each reflected point lands on the
mesh, and re-estimate the plane from the pairs (normal = the direction the
pairs run in, point = their midpoints); a few rounds settle it. The RMS of
the reflected points' distance to the mesh says how symmetric the part is.
The idea is ScanRuler's (reflect + ICP); the closest point is taken on the
mesh itself, so a CAD export's exact symmetry reads at float level.

Using it (`symmetrise_profiles`): in a slab's section plane a mirror plane
that contains the extrusion axis is a line. Every fitted line, arc and
circle is paired with its mirror twin, and each pair is replaced by the
mean of itself and the twin's reflection; a piece that is its own twin (a
line square to the mirror line, an arc centred on it) is made symmetric on
its own. Pairs are only taken within a tight tolerance, so a part that is
nearly but not quite symmetric keeps its asymmetry. Junctions are re-solved
by the caller.
"""
import numpy as np


SAMPLE = 3000               # surface points used to read the mirror plane
ITERS = 8
# a part is taken as symmetric when the mirrored surface lies this close to
# the mesh (RMS over the inlier pairs), and nearly all of it pairs up
ACCEPT_RMS = 0.01           # mm, CAD exports (exact)
ACCEPT_INLIERS = 0.95


def _samples(mesh, n=SAMPLE, seed=0):
    """Deterministic area-weighted surface samples and their face normals."""
    import trimesh
    pts, fi = trimesh.sample.sample_surface(mesh, n, seed=seed)
    return np.asarray(pts, float), np.asarray(mesh.face_normals[fi], float)


def _reflect(P, n, d):
    return P - 2.0 * ((P @ n) - d)[:, None] * n


# A sample only says where the mirror plane is when its surface faces along
# the mirror normal: a point on the top face of a plate mirrors onto the
# same face whatever the offset (point-to-plane ICP, in effect)
FACING_MIN = 0.3


def refine_plane(mesh, n, d, sample, iters=ITERS, floor=ACCEPT_RMS):
    """ICP on the mirror plane (unit normal n, offset d: x.n = d) from the
    samples (points, normals). Returns (n, d, rms, inlier fraction), both
    read over the samples that face along n: the RMS of their mirror
    images' distance to the mesh over the inliers (within max(3 x median,
    `floor`)), and the inliers' share."""
    from trimesh.proximity import closest_point
    P, N = sample
    n = np.asarray(n, float) / np.linalg.norm(n)
    rms, frac = np.inf, 0.0
    for _ in range(iters):
        w = np.abs(N @ n)
        facing = w >= FACING_MIN
        if facing.sum() < 3:
            break
        R = _reflect(P[facing], n, d)
        Q, dist, _ = closest_point(mesh, R)
        keep = dist <= max(3.0 * float(np.median(dist)), floor)
        rms = float(np.sqrt(np.mean(dist[keep] ** 2))) if keep.any() else np.inf
        frac = float(keep.mean())
        # pairs P <-> Q (Q is where P's mirror image really is): the plane
        # bisects each pair
        Pk, Qk, wk = P[facing][keep], Q[keep], w[facing][keep]
        D = Pk - Qk
        L = np.linalg.norm(D, axis=1)
        ok = L > 1e-9
        if ok.sum() < 3:
            break
        Dn = D[ok] / L[ok, None]
        Dn *= np.sign(Dn @ n)[:, None]
        n_new = (Dn * (wk * L)[ok][:, None]).sum(axis=0)
        n_new /= np.linalg.norm(n_new)
        d_new = float(np.average((0.5 * (Pk + Qk)) @ n_new, weights=wk))
        moved = np.linalg.norm(n_new - n) + abs(d_new - d)
        n, d = n_new, d_new
        if moved < 1e-10:
            break
    return n, d, rms, frac


def mirror_plane(mesh, normals, accept_rms=ACCEPT_RMS, accept_inliers=ACCEPT_INLIERS):
    """Best mirror plane among the candidate `normals` (each started
    through the samples' centroid), as {'normal', 'd', 'rms', 'inliers'},
    or None when no candidate reads symmetric."""
    sample = _samples(mesh)
    c = sample[0].mean(axis=0)
    best = None
    for n0 in normals:
        n0 = np.asarray(n0, float)
        if np.linalg.norm(n0) < 1e-12:
            continue
        n0 = n0 / np.linalg.norm(n0)
        n, d, rms, frac = refine_plane(mesh, n0, float(c @ n0), sample, floor=accept_rms)
        if best is None or (frac, -rms) > (best['inliers'], -best['rms']):
            best = {'normal': n, 'd': d, 'rms': rms, 'inliers': frac}
    if best is None or best['rms'] > accept_rms or best['inliers'] < accept_inliers:
        return None
    return best


def axis_mirror_plane(mesh, axis, basis, accept_rms=ACCEPT_RMS):
    """A mirror plane that contains the extrusion `axis` (its normal is
    square to the axis), tried along the section frame's two directions and
    the in-plane principal directions of the vertices. Planes that lean off
    the axis by more than 0.05 deg are refused: the profile could not be
    symmetric about them."""
    axis = np.asarray(axis, float)
    bx, by = np.asarray(basis[:3, 0], float), np.asarray(basis[:3, 1], float)
    V = np.asarray(mesh.vertices, float)
    uv = np.column_stack([V @ bx, V @ by])
    uv -= uv.mean(axis=0)
    _, _, vt = np.linalg.svd(uv, full_matrices=False)
    cands = [bx, by] + [vt[k, 0] * bx + vt[k, 1] * by for k in range(2)]
    found = []
    for n0 in cands:
        pl = mirror_plane(mesh, [n0], accept_rms=accept_rms)
        if pl is None or abs(pl['normal'] @ axis) > np.sin(np.radians(0.05)):
            continue
        # the same plane found again (from another start, or flipped)
        if any(abs(abs(pl['normal'] @ f['normal']) - 1) < 1e-4
               and abs(pl['d'] * np.sign(pl['normal'] @ f['normal']) - f['d']) < 0.05
               for f in found):
            continue
        found.append(pl)
    return found


# ---------- profiles ----------

def line_2d(plane, basis):
    """The mirror plane's trace in the section frame: (unit normal m in
    2-D, offset e) with the line u.m = e. The plane contains the axis, so
    its normal has no axis component."""
    n = np.asarray(plane['normal'], float)
    bx, by = np.asarray(basis[:3, 0], float), np.asarray(basis[:3, 1], float)
    m = np.array([n @ bx, n @ by])
    L = np.linalg.norm(m)
    return m / L, float(plane['d']) / L


def _mir(p, m, e):
    p = np.asarray(p, float)
    return p - 2.0 * (p @ m - e) * m


def _prim_key_pts(p):
    if p['type'] == 'line':
        return [np.asarray(p['p0'], float), np.asarray(p['p1'], float)]
    if p.get('full'):
        return [np.asarray(p['center'], float)]
    return [np.asarray(p['center'], float), np.asarray(p['p0'], float), np.asarray(p['p1'], float)]


def _mirror_prim(p, m, e):
    """The reflection of primitive p (a reflection reverses direction: a
    line's ends swap, an arc's sense flips and its ends swap)."""
    q = dict(p)
    if p['type'] == 'line':
        q['p0'], q['p1'] = _mir(p['p1'], m, e), _mir(p['p0'], m, e)
    else:
        q['center'] = _mir(p['center'], m, e)
        if not p.get('full'):
            q['p0'], q['p1'] = _mir(p['p1'], m, e), _mir(p['p0'], m, e)
    return q


def _match(a, b, tol):
    """Largest distance between a's key points and the mirrored b's (same
    order), or inf when they cannot be twins."""
    if a['type'] != b['type'] or bool(a.get('full')) != bool(b.get('full')):
        return np.inf
    if a['type'] == 'arc' and abs(float(a['r']) - float(b['r'])) > tol:
        return np.inf
    if a['type'] == 'arc' and not a.get('full') and bool(a.get('ccw', True)) != bool(b.get('ccw', True)):
        # reflected and run end to start, an arc keeps its sense: a twin
        # in the same ring turns the same way
        return np.inf
    pa, pb = _prim_key_pts(a), _prim_key_pts(b)
    return max(float(np.linalg.norm(x - y)) for x, y in zip(pa, pb))


def snap_line_to_frame(m, frame_rad, max_deg=0.3):
    """The 2-D mirror normal m turned exactly onto the profile's dominant
    frame (an angle, see profile_fit.dominant_frame) when within `max_deg`:
    the mirror line of a squared-up part is squared up too, or averaging
    a piece with its twin would tilt frame-snapped lines by the ICP's
    hair of error."""
    if frame_rad is None:
        return m
    ang = np.arctan2(m[1], m[0])
    for k in range(4):
        t = frame_rad + k * np.pi / 2
        dd = (ang - t + np.pi) % (2 * np.pi) - np.pi
        if abs(dd) < np.radians(max_deg):
            return np.array([np.cos(t), np.sin(t)])
    return m


def _pieces(rings_list):
    prims = []
    for outer, holes in rings_list:
        for ring in [outer] + list(holes):
            if isinstance(ring, dict):
                prims.append(ring)
            else:
                prims.extend(ring)
    return prims


def _pairs(prims, m, e, tol):
    """Greedy twin pairs (i, j) (i == j: its own twin) within `tol`."""
    used, out = set(), []
    for i, a in enumerate(prims):
        if i in used:
            continue
        ma = _mirror_prim(a, m, e)
        best, bj = tol, None
        for j, b in enumerate(prims):
            if j in used:
                continue
            dist = _match(b, ma, tol)
            if dist <= best:
                best, bj = dist, j
        if bj is not None:
            used.update({i, bj})
            out.append((i, bj))
    return out


def refine_offset(rings_list_all, m, e, tol):
    """The mirror line's offset re-read from the fitted pieces: the median
    over twin pairs of their key points' midpoints along m. Pieces are
    fitted on exact vertices, so on a CAD export this is far tighter than
    the surface sampling that found the plane."""
    vals = []
    for rings_list in rings_list_all:
        prims = _pieces(rings_list)
        for i, j in _pairs(prims, m, e, tol):
            for x, y in zip(_prim_key_pts(prims[i]), _prim_key_pts(_mirror_prim(prims[j], m, e))):
                # x and the reflection y of its twin's point should coincide;
                # their difference along m is twice the offset error
                vals.append(e + 0.5 * float((x - y) @ m))
    return float(np.median(vals)) if vals else e


def symmetrise_profiles(rings_list, m, e, tol):
    """Make the fitted profiles of one slab (its list of (outer, holes))
    exactly symmetric about the 2-D line u.m = e, in place, pairing pieces
    whose mirror images agree within `tol`. Returns the number of pieces
    moved. Pieces with no twin are left as fitted."""
    prims = _pieces(rings_list)
    moved = 0
    for i, bj in _pairs(prims, m, e, tol):
        a, b = prims[i], prims[bj]
        mb = _mirror_prim(b, m, e)
        # a := mean(a, mirror(b)); b := mirror(a)
        if a['type'] == 'line':
            a['p0'] = 0.5 * (np.asarray(a['p0'], float) + mb['p0'])
            a['p1'] = 0.5 * (np.asarray(a['p1'], float) + mb['p1'])
        else:
            a['center'] = 0.5 * (np.asarray(a['center'], float) + mb['center'])
            a['r'] = 0.5 * (float(a['r']) + float(b['r']))
            if not a.get('full'):
                a['p0'] = 0.5 * (np.asarray(a['p0'], float) + mb['p0'])
                a['p1'] = 0.5 * (np.asarray(a['p1'], float) + mb['p1'])
        if bj != i:
            mirrored = _mirror_prim(a, m, e)
            for k in ('p0', 'p1', 'center', 'r'):
                if k in mirrored:
                    b[k] = mirrored[k]
        a['symmetric'] = b['symmetric'] = True
        moved += 1 if bj == i else 2
    return moved
