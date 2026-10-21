"""Plane sections of a triangle mesh, and clean sketch curves fitted to them.

This is the engine behind "Create Mesh Section Sketch" + "Fit Curves to
Mesh Section" done by hand in Fusion: cut the mesh with a plane, chain the
crossing segments into closed loops, then redraw every loop as lines and
arcs where they hold the tolerance and as a fitted spline where they do
not (the domes of a controller shell, a scanned handle).

numpy only, on purpose: the same file is copied into the Fusion add-in,
which has no shapely or trimesh. The pipeline's sliced-loft mode uses the
plane cut too (`stl_to_solid.sliced_loft`), so every slice of a loft and every
sketch in the generated Fusion script comes from this one cutter.

Coordinates: `section_loops` returns 2-D points in an in-plane frame (u, v)
with `to_3d(xy)` = origin + u*x + v*y. Outer loops are counter-clockwise,
holes clockwise, holes attached to the outer loop that contains them.

A leaky mesh (separate surface patches, a scan) cuts into open chains as
well as closed loops. `section_curves` keeps them apart: closed loops are
nested and fitted as loops, open chains are fitted as open curves and drawn
open, exactly as Fusion's Create Mesh Section Sketch draws them, and free
ends closer than `join_mm` are joined first so an outline that is only
broken by hairline cracks between patches comes back closed. `section_loops`
(the loft's cutter) keeps its older behaviour of closing every chain by its
chord.
"""
import math

import numpy as np

from .profile_fit import (_arc_mid, segment_polyline, snap_profile, solve_junctions,
                          try_full_circle)


# ---------------------------------------------------------------------------
# plane cut

def plane_basis(normal):
    """Orthonormal (u, v, n): n is the unit normal, u the in-plane direction
    closest to the world axis least aligned with n, v = n x u. Deterministic,
    so two slices with the same normal share a frame."""
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    k = int(np.argmin(np.abs(n)))
    a = np.zeros(3)
    a[k] = 1.0
    u = a - n * (a @ n)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return u, v, n


def cut_segments(V, F, origin, normal):
    """Every crossing of the plane with a triangle, as a segment between the
    two crossing points. Returns (P, S): P (m, 3) crossing points, one per
    crossed mesh edge, and S (k, 2) indices into P, one row per crossed
    triangle. Points come from the edge (sorted vertex pair), so two
    triangles sharing an edge share the point exactly and loops chain
    without any rounding."""
    V = np.asarray(V, float)
    F = np.asarray(F, np.int64)
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    d = (V - np.asarray(origin, float)) @ n
    # a vertex exactly on the plane counts as the positive side, so every
    # crossed triangle has exactly two crossing edges
    side = d >= 0
    s = side[F]
    crossed = ~(s.all(1) | (~s).all(1))
    if not crossed.any():
        return np.zeros((0, 3)), np.zeros((0, 2), np.int64)
    T = F[crossed]
    st = s[crossed]
    # edges of each crossed triangle: (0,1) (1,2) (2,0); crossing when the
    # two ends differ in side
    E = np.stack([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]], axis=1)   # (k, 3, 2)
    X = np.stack([st[:, 0] != st[:, 1], st[:, 1] != st[:, 2], st[:, 2] != st[:, 0]], axis=1)
    assert (X.sum(1) == 2).all()
    Ek = E[X].reshape(-1, 2, 2)                                        # (k, 2, 2)
    Ek = np.sort(Ek, axis=2)
    flat = Ek.reshape(-1, 2)
    uniq, inv = np.unique(flat, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    a, b = uniq[:, 0], uniq[:, 1]
    da, db = d[a], d[b]
    t = da / (da - db)
    P = V[a] + (V[b] - V[a]) * t[:, None]
    S = inv.reshape(-1, 2)
    return P, S


def chain_loops(S):
    """Chain segments (rows of point-index pairs) into loops. Each point of
    a closed mesh section is used by exactly two segments; a point used
    once (an open, leaky mesh) starts an open chain that is returned as
    well, so nothing is silently dropped. Returns a list of (index list,
    closed); a closed loop does not repeat its first index, an open chain
    has at least two points."""
    if len(S) == 0:
        return []
    nbrs = {}
    for k, (i, j) in enumerate(S):
        nbrs.setdefault(int(i), []).append((int(j), k))
        nbrs.setdefault(int(j), []).append((int(i), k))
    used = np.zeros(len(S), bool)
    loops = []
    # open chains first (their ends are the points with one segment)
    starts = [p for p, l in nbrs.items() if len(l) == 1] + list(nbrs)
    for start in starts:
        for (_, k0) in nbrs[start]:
            if used[k0]:
                continue
            loop = [start]
            cur, k = start, k0
            closed = False
            while True:
                used[k] = True
                i, j = S[k]
                nxt = int(j) if int(i) == cur else int(i)
                if nxt == start:
                    closed = True
                    break
                loop.append(nxt)
                cur = nxt
                step = next(((q, kk) for (q, kk) in nbrs[cur] if not used[kk]), None)
                if step is None:
                    break
                k = step[1]
            if len(loop) >= (3 if closed else 2):
                loops.append((loop, closed))
    return loops


def join_chains(chains, xy, join_mm):
    """Join the free ends of open chains that sit within `join_mm` of each
    other: the closest pair of ends first, then the next, until no pair is
    that close. A chain whose own two ends are the closest pair becomes a
    closed loop. `chains` is `chain_loops` output, `xy` the 2-D point per
    index. Returns (chains, joins made, largest gap bridged). join_mm <= 0
    returns the input untouched."""
    if join_mm <= 0 or not chains:
        return chains, 0, 0.0
    done = [(list(c), True) for c, cl in chains if cl]
    open_ = [list(c) for c, cl in chains if not cl]
    n_join, max_gap = 0, 0.0
    while open_:
        ends = [(ci, e) for ci in range(len(open_)) for e in (0, 1)]
        E = np.array([xy[open_[ci][0 if e == 0 else -1]] for ci, e in ends])
        if len(E) < 2:
            break
        D = np.linalg.norm(E[:, None, :] - E[None, :, :], axis=2)
        np.fill_diagonal(D, np.inf)
        i, j = np.unravel_index(int(np.argmin(D)), D.shape)
        gap = float(D[i, j])
        if gap > join_mm:
            break
        (ci, si), (cj, sj) = ends[i], ends[j]
        n_join += 1
        max_gap = max(max_gap, gap)
        if ci == cj:
            done.append((open_.pop(ci), True))
            continue
        a, b = open_[ci], open_[cj]
        if si == 0:                 # join at a's end
            a = a[::-1]
        if sj == 1:                 # ... to b's start
            b = b[::-1]
        if gap < 1e-9:              # coincident ends: one point, not two
            b = b[1:]
        for k in sorted((ci, cj), reverse=True):
            open_.pop(k)
        open_.append(a + b)
    return done + [(c, False) for c in open_], n_join, max_gap


def _big_chains(chains, min_frac=0.05):
    """The open chains that carry the outline: at least `min_frac` of
    the total open length. Scraps (a loose patch of a leaky mesh) are left
    out, so they neither get stitched into the outline nor make the gap
    that would close it look huge."""
    cs = [np.asarray(c, float) for c in chains if len(c) >= 2]
    total = sum(_length(c, False) for c in cs)
    return [c for c in cs if _length(c, False) >= min_frac * total]


def closing_gap(chains):
    """The Join gaps value that would close these open 2-D chains (point
    arrays): join_chains' closest-ends-first rule run with no limit on the
    chains that carry the outline (_big_chains), and the widest gap it
    bridges. 0.0 for no chains. Said to the user when a slice's outline
    stays open (a mesh hole wider than Join gaps)."""
    open_ = _big_chains(chains)
    worst = 0.0
    while open_:
        E = np.array([c[k] for c in open_ for k in (0, -1)])
        D = np.linalg.norm(E[:, None, :] - E[None, :, :], axis=2)
        np.fill_diagonal(D, np.inf)
        i, j = np.unravel_index(int(np.argmin(D)), D.shape)
        worst = max(worst, float(D[i, j]))
        ci, cj = i // 2, j // 2
        if ci == cj:
            open_.pop(ci)
            continue
        a = open_[ci] if i % 2 else open_[ci][::-1]
        b = open_[cj] if j % 2 == 0 else open_[cj][::-1]
        for k in sorted((ci, cj), reverse=True):
            open_.pop(k)
        open_.append(np.vstack([a, b]))
    return worst


def outline_open(st):
    """True when a slice's outline stayed open: its longest open piece is
    longer than its longest closed loop (stats open_max, loop_max)."""
    return st.get('open_max', 0.0) > st.get('loop_max', 0.0)


def open_outline_note(slices, join_mm):
    """One sentence for the slices whose outline stayed open: `slices` is
    [(label, stats)] of a stack; a slice counts when its longest open
    piece is longer than its longest closed loop (outline_open), which
    loses its slab. None when there are none."""
    bad = [(lab, st) for lab, st in slices if outline_open(st)]
    if not bad:
        return None
    need = max(st.get('close_gap', 0.0) for _, st in bad)
    need = math.ceil(need * 2.0 - 1e-9) / 2.0          # the half millimetre at or above it
    labs = ', '.join(lab for lab, _ in bad[:8]) + (', ...' if len(bad) > 8 else '')
    head = ('the outline did not close in {} slice{} ({}): the mesh has gaps wider than Join gaps '
            '({:g} mm) there, so {} no profile and no slab. '.format(
                len(bad), '' if len(bad) == 1 else 's', labs, join_mm,
                'that slice has' if len(bad) == 1 else 'those slices have'))
    if need > 10.0:                                     # the same rule as the web summary
        # a Join gaps that wide would stitch unrelated pieces together
        return head + ('The gap is {:g} mm, too wide to join safely: start or end the stack '
                       'clear of {}, or untick Outline only to see the open pieces.'.format(
                           need, 'it' if len(bad) == 1 else 'them'))
    return head + 'Join gaps {:g} mm would close {}.'.format(need, 'it' if len(bad) == 1 else 'them')


def _bridge_crosses(a, b, chain, slack=0.0):
    """Does the straight bridge a -> b properly cross any segment of the
    open polyline `chain` (its own segments only, none made up between
    its last and first point)? With `slack`, crossings where the chain
    pokes no further than `slack` past the bridge (on the side away from
    the bulk of the chain, within the bridge's span) are a graze, not a
    wall in the way: 0.3 mm bumps on the RC-N1 controller's underside
    under a 32 mm chord."""
    if len(chain) < 2:
        return False
    A, B = chain[:-1], chain[1:]
    r = b - a
    s_ = B - A
    rxs = r[0] * s_[:, 1] - r[1] * s_[:, 0]
    qp = A - a
    with np.errstate(divide='ignore', invalid='ignore'):
        t = (qp[:, 0] * s_[:, 1] - qp[:, 1] * s_[:, 0]) / rxs
        u = (qp[:, 0] * r[1] - qp[:, 1] * r[0]) / rxs
    hit = (np.abs(rxs) > 1e-12) & (t > 1e-6) & (t < 1 - 1e-6) & (u > 1e-6) & (u < 1 - 1e-6)
    if slack > 0 and hit.any():
        rl = max(float(np.linalg.norm(r)), 1e-30)
        nrm = np.array([-r[1], r[0]]) / rl
        side = (chain - a) @ nrm
        along = (chain - a) @ (r / rl)
        bulk = 1.0 if side.mean() >= 0 else -1.0
        past = (side * bulk < 0) & (along > 0) & (along < rl)
        depth = float(np.max(-side[past] * bulk)) if past.any() else 0.0
        # and the far end of every segment that crosses it, wherever that
        # end lies: a wall cutting the bridge and leaving its span on a
        # slant has its far vertex outside the span (review F2)
        sa, sb = side[:-1][hit], side[1:][hit]
        far = np.where(sa * bulk < 0, -sa * bulk, -sb * bulk)
        if len(far):
            depth = max(depth, float(far.max()))
        return depth > slack
    return bool(hit.any())


def _close_on_itself(c, gap):
    """An open chain whose end comes down onto its own outline (a grate
    bar's wall stopping on the bottom skin it belongs to: a T, not a gap):
    the loop from the touch point to that end, closed by a hop of at most
    `gap`; the stub beyond the touch point is left off. Of every place
    either end could close (segments far enough along the chain that a
    real loop goes round first) the one enclosing the most area wins: an
    end can also touch a grate bar a few mm along, which would give a
    sliver. Returns (loop, hop length) or (None, None)."""
    n = len(c)
    if n < 4:
        return None, None
    seg = np.linalg.norm(np.diff(c, axis=0), axis=1)
    along = np.concatenate([[0.0], np.cumsum(seg)])
    A, B = c[:-1], c[1:]
    d = B - A
    L2 = np.maximum(np.einsum('ij,ij->i', d, d), 1e-30)
    cr = A[:, 0] * B[:, 1] - A[:, 1] * B[:, 0]         # shoelace terms, segment k
    pre = np.concatenate([[0.0], np.cumsum(cr)])        # pre[k] = sum of terms < k
    best, best_area, best_hop = None, 0.0, None
    for last in (True, False):
        e = c[-1] if last else c[0]
        t = np.clip(np.einsum('ij,ij->i', e - A, d) / L2, 0.0, 1.0)
        foot = A + t[:, None] * d
        dist = np.linalg.norm(foot - e, axis=1)
        s_at = along[:-1] + t * seg
        ok = (dist <= gap) & ((along[-1] - s_at > 2 * gap) if last else (s_at > 2 * gap))
        ks = np.flatnonzero(ok)
        if not len(ks):
            continue
        f = foot[ks]
        if last:
            # loop: f -> c[k+1] -> ... -> c[n-1] -> f
            nx = c[ks + 1]
            area2 = (f[:, 0] * nx[:, 1] - f[:, 1] * nx[:, 0]) + (pre[n - 1] - pre[ks + 1]) \
                + (e[0] * f[:, 1] - e[1] * f[:, 0])
        else:
            # loop: c[0] -> ... -> c[k] -> f -> c[0]
            ck = c[ks]
            area2 = pre[ks] + (ck[:, 0] * f[:, 1] - ck[:, 1] * f[:, 0]) + (f[:, 0] * e[1] - f[:, 1] * e[0])
        m = int(np.argmax(np.abs(area2)))
        if abs(area2[m]) / 2.0 > best_area:
            k = int(ks[m])
            loop = np.vstack([foot[k], c[k + 1:]]) if last else np.vstack([c[:k + 1], foot[k]])
            best, best_area, best_hop = loop, abs(area2[m]) / 2.0, float(dist[k])
    return (best, best_hop) if best is not None else (None, None)


def close_open_outline(chains, max_bridge_frac=0.25, gap=1.0, slack=0.5):
    """Close an outline the mesh left open (a vent grate's slots: the
    RC-N1 controller's underside has no skin along a 34 mm slot, so the
    cut is one open chain with its ends 34 mm apart and no Join gaps value
    closes it sensibly). The chains that carry the outline (_big_chains)
    are joined closest ends first with no distance limit; the last chain
    is closed on itself where an end lands on its own outline
    (_close_on_itself, a hop no longer than `gap`) or by its chord,
    whichever valid closing encloses more area: straight bridges across
    the holes, which fill them as a solid would be. Refused (None, [])
    when any bridge longer than `gap` crosses the loop it ends up in by
    more than `slack` (hops no longer than `gap` are not checked, as joins
    are not), or the bridges add up to more than `max_bridge_frac`
    of the loop. Returns (closed loops, bridge lengths)."""
    open_ = _big_chains(chains)
    if not open_:
        return None, []
    done, bridges, long_ = [], [], []          # long_: (a, b) of bridges > gap

    def bridge(a, b):
        L = float(np.linalg.norm(b - a))
        bridges.append(L)
        if L > gap:
            long_.append((a.copy(), b.copy()))

    def valid(loops, brs, longs):
        total = sum(_length(c, True) for c in loops)
        if any(len(c) < 3 for c in loops) or sum(brs) > max_bridge_frac * total:
            return False
        return not any(_bridge_crosses(a, b, np.vstack([c, c[:1]]), slack)
                       for a, b in longs for c in loops)

    while open_:
        if len(open_) == 1:
            # the last chain: closed on itself (an end landing on its own
            # outline) or by its chord, whichever valid way encloses more;
            # an end can touch a grate bar a few mm along and give a 37 mm2
            # loop where the chord gives the 5000 mm2 outline
            c = open_.pop()
            options = []
            loop, hop = _close_on_itself(c, gap)
            if loop is not None:
                options.append((done + [loop], bridges + [hop], list(long_)))
            L = float(np.linalg.norm(c[-1] - c[0]))
            options.append((done + [c], bridges + [L],
                            long_ + ([(c[-1].copy(), c[0].copy())] if L > gap else [])))
            ok = [o for o in options if valid(*o)]
            if not ok:
                return None, []
            area = [sum(abs(signed_area(x)) for x in o[0]) for o in ok]
            # closing on itself wins a near tie: the chord there runs back
            # along the stub it leaves, a zero-width spike
            if loop is not None and len(ok) == 2 and area[0] >= 0.95 * area[1]:
                return ok[0][0], ok[0][1]
            best = ok[int(np.argmax(area))]
            return best[0], best[1]
        E = np.array([c[k] for c in open_ for k in (0, -1)])
        D = np.linalg.norm(E[:, None, :] - E[None, :, :], axis=2)
        np.fill_diagonal(D, np.inf)
        i, j = np.unravel_index(int(np.argmin(D)), D.shape)
        ci, cj = i // 2, j // 2
        if ci == cj:                                  # a chain closing on itself
            c = open_.pop(ci)
            bridge(c[-1], c[0])
            done.append(c)
            continue
        a = open_[ci] if i % 2 else open_[ci][::-1]
        b = open_[cj] if j % 2 == 0 else open_[cj][::-1]
        bridge(a[-1], b[0])
        for k in sorted((ci, cj), reverse=True):
            open_.pop(k)
        open_.append(np.vstack([a, b]))
    if not valid(done, bridges, long_):
        return None, []
    return done, bridges


def _length(xy, closed):
    xy = np.asarray(xy, float)
    if len(xy) < 2:
        return 0.0
    if closed:
        xy = np.vstack([xy, xy[:1]])
    return float(np.linalg.norm(np.diff(xy, axis=0), axis=1).sum())


def signed_area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def point_in_loop(pt, xy):
    """Even-odd ray cast of one point against a closed 2-D loop."""
    x, y = pt
    x0, y0 = xy[:, 0], xy[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    cond = (y0 > y) != (y1 > y)
    with np.errstate(divide='ignore', invalid='ignore'):
        xs = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return bool(np.sum(cond & (x < xs)) % 2)


def nest_loops(loops_xy):
    """Group closed 2-D loops into (outer, [holes]) by containment depth:
    even depth is material, odd depth a hole in the smallest containing
    even loop. Loops come back oriented: outers CCW, holes CW."""
    n = len(loops_xy)
    areas = [abs(signed_area(l)) for l in loops_xy]
    order = sorted(range(n), key=lambda i: -areas[i])       # big first
    parent = [-1] * n
    depth = [0] * n
    for idx, i in enumerate(order):
        pt = loops_xy[i][0]
        # the smallest loop that contains this one: candidates are bigger
        # loops (earlier in `order`), checked smallest first
        for j in reversed(order[:idx]):
            if areas[j] > areas[i] and point_in_loop(pt, loops_xy[j]):
                parent[i] = j
                depth[i] = depth[j] + 1
                break
    out = {}
    for i in range(n):
        if depth[i] % 2 == 0:
            xy = loops_xy[i]
            out[i] = (xy if signed_area(xy) > 0 else xy[::-1].copy(), [])
    for i in range(n):
        if depth[i] % 2 == 1 and parent[i] in out:
            xy = loops_xy[i]
            out[parent[i]][1].append(xy if signed_area(xy) < 0 else xy[::-1].copy())
    return [out[i] for i in sorted(out, key=lambda i: -areas[i])]


def _cut_xy(V, F, origin, normal):
    u, v, n = plane_basis(normal)
    o = np.asarray(origin, float)
    P, S = cut_segments(V, F, o, n)
    xy_all = np.stack([(P - o) @ u, (P - o) @ v], axis=1) if len(P) else np.zeros((0, 2))
    return xy_all, S, (o, u, v, n)


def _dedupe(xy):
    """Drop consecutive duplicates (a crossing at a shared vertex)."""
    keep = np.ones(len(xy), bool)
    keep[1:] = np.linalg.norm(xy[1:] - xy[:-1], axis=1) > 1e-9
    return xy[keep]


def section_loops(V, F, origin, normal, min_area=1e-6):
    """Cut the mesh (V, F) with the plane (origin, normal), every chain
    taken as a closed loop (the sliced loft's cutter).

    Returns (loops, frame): loops is a list of (outer_xy, [hole_xy, ...])
    with 2-D points in the plane frame, outers CCW and holes CW, largest
    outer first; frame is (origin, u, v, n) with to_3d(xy) = origin + u*x
    + v*y. Loops of less than `min_area` (mm^2) are dropped as numerical
    dust. Open chains (a leaky mesh) are kept as loops too: closed by
    their chord. For sketches use `section_curves`, which keeps them
    open."""
    xy_all, S, frame = _cut_xy(V, F, origin, normal)
    loops = []
    for idx, _closed in chain_loops(S):
        xy = _dedupe(xy_all[idx])
        if len(xy) >= 3 and abs(signed_area(xy)) >= min_area:
            loops.append(xy)
    return nest_loops(loops), frame


def section_curves(V, F, origin, normal, min_area=1e-6, join_mm=0.0):
    """Cut the mesh (V, F) with the plane (origin, normal), keeping open
    chains open, as Fusion's Create Mesh Section Sketch does.

    Returns (loops, open_chains, frame, joins): loops as `section_loops`
    gives them (closed chains only, nested, outers CCW, holes CW);
    open_chains a list of 2-D polylines, longest first, never closed; frame
    (origin, u, v, n); joins {'joins': ends joined, 'max_gap': largest gap
    bridged}. Free ends within `join_mm` of each other are joined first
    (`join_chains`), so a shell whose patches leave hairline cracks comes
    back as one closed outline; 0 joins nothing."""
    xy_all, S, frame = _cut_xy(V, F, origin, normal)
    chains, n_join, max_gap = join_chains(chain_loops(S), xy_all, join_mm)
    loops, opens = [], []
    for idx, closed in chains:
        xy = _dedupe(xy_all[idx])
        if closed:
            if len(xy) >= 3 and abs(signed_area(xy)) >= min_area:
                loops.append(xy)
        elif len(xy) >= 2:
            opens.append(xy)
    opens.sort(key=lambda q: -float(np.linalg.norm(np.diff(q, axis=0), axis=1).sum()))
    return nest_loops(loops), opens, frame, {'joins': n_join, 'max_gap': max_gap}


def to_3d(xy, frame):
    o, u, v, _ = frame
    xy = np.asarray(xy, float)
    return o + np.outer(xy[:, 0], u) + np.outer(xy[:, 1], v)


# ---------------------------------------------------------------------------
# sliver trimming

def _crossings(P):
    """Proper crossings between non-adjacent segments of the closed
    polyline P: list of (i, j, X) with i < j, segment k = P[k] -> P[k+1]."""
    n = len(P)
    A = P
    B = np.roll(P, -1, axis=0)
    out = []
    for i in range(n - 2):
        j = np.arange(i + 2, n if i > 0 else n - 1)
        p, r = A[i], B[i] - A[i]
        q, s_ = A[j], B[j] - A[j]
        rxs = r[0] * s_[:, 1] - r[1] * s_[:, 0]
        qp = q - p
        with np.errstate(divide='ignore', invalid='ignore'):
            t = (qp[:, 0] * s_[:, 1] - qp[:, 1] * s_[:, 0]) / rxs
            u = (qp[:, 0] * r[1] - qp[:, 1] * r[0]) / rxs
        hit = (np.abs(rxs) > 1e-12) & (t > 1e-9) & (t < 1 - 1e-9) & (u > 1e-9) & (u < 1 - 1e-9)
        for jj, tt in zip(j[hit], t[hit]):
            out.append((i, int(jj), p + tt * r))
    return out


def _sub_stats(Q):
    """(length, |area|) of the closed polygon Q (its last point closes to
    its first)."""
    L = float(np.linalg.norm(np.diff(np.vstack([Q, Q[:1]]), axis=0), axis=1).sum())
    return L, abs(signed_area(Q))


def trim_slivers(xy, w, max_rounds=50):
    """Cut the slivers out of one closed loop: a stretch that leaves a
    point and comes back within `w` of it, or that crosses itself, and
    encloses a strip thinner than `w` (area < w * length / 2). That is the
    double skin of a leaky mesh (two walls a hair apart, joined at their
    ends) and the little bow-ties where two patches overlap: they draw as
    hairpins and self-crossings that Fusion turns into sliver profiles.
    The loop is otherwise untouched; a real slot wider than `w` stays.
    A hairpin must be at least 2*w long to count (shorter ones are just
    dense points on a bend); a twist of any length is removed, since the
    smallest ones (0.02 mm curls where two patches overlap) are exactly
    what makes Fusion refuse to loft the profile.
    A loop that is thin all over (its mean width, 2*area/perimeter, under
    `w`: the 1.3 mm strip left beside a cross hole when `w` is 1.5) is a
    feature, not a sliver on a loop, and is left whole; cutting it would
    only leave a stub of it.
    Returns (xy, slivers removed)."""
    P = np.asarray(xy, float).copy()
    removed = 0
    for _ in range(max_rounds):
        n = len(P)
        if n < 6:
            break
        Pn = np.roll(P, -1, axis=0)
        seg = np.linalg.norm(Pn - P, axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = cum[-1]
        # running shoelace sum: the area of the stretch i..j closed by its
        # chord is 0.5 * (S[j] - S[i] + (xj*yi - xi*yj)), in O(1)
        S = np.concatenate([[0.0], np.cumsum(P[:, 0] * Pn[:, 1] - Pn[:, 0] * P[:, 1])])
        if total <= 0 or abs(S[n]) / total < w:          # 2 * area / perimeter < w
            break
        cands = []                                    # (L, lo, hi, kind, X)
        # stretches whose two ends nearly meet
        D = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
        near = np.argwhere(np.triu(D < w, k=3))
        if len(near):
            i, j = near[:, 0], near[:, 1]
            Lf = cum[j] - cum[i]
            d = D[i, j]
            fwd = Lf <= total - Lf
            L = np.where(fwd, Lf, total - Lf)
            ok = (L >= 3.0 * np.maximum(d, 1e-9)) & (L >= 2.0 * w) & (L <= total - 2.0 * w)
            area_f = 0.5 * np.abs(S[j] - S[i] + P[j, 0] * P[i, 1] - P[i, 0] * P[j, 1])
            tot_area = 0.5 * abs(S[n])
            area = np.where(fwd, area_f, np.abs(tot_area - area_f))
            ok &= area < 0.5 * w * L
            for k in np.nonzero(ok)[0]:
                cands.append((float(L[k]), int(i[k]), int(j[k]), 'pin' if fwd[k] else 'pin_wrap', None))
        # twists: the loop between two crossing segments
        for i, j, X in _crossings(P):
            if j - i <= n - (j - i):
                Q = np.vstack([[X], P[i + 1:j + 1]])
                kind = 'twist'
            else:
                Q = np.vstack([[X], P[j + 1:], P[:i + 1]])
                kind = 'twist_wrap'
            L, area = _sub_stats(Q)
            if L > total - 2.0 * w:
                continue
            if area < 0.5 * w * L:
                cands.append((L, i, j, kind, X))
        if not cands:
            break
        # take every candidate whose index span is free of the ones already
        # taken, longest first: a hairpin is a candidate at every depth
        # (its tip, its tip plus a bit, ...) and the whole of it must go,
        # not the tip round after round until a stub under 2*w is left
        cands.sort(key=lambda c: -c[0])
        taken = np.zeros(n, bool)
        drop = np.zeros(n, bool)                       # points to delete
        insert = {}                                    # index -> point to put after it
        keep_only = None
        for L, i, j, kind, X in cands:
            if kind.endswith('_wrap'):
                if any(taken) or keep_only is not None:
                    continue
                keep_only = (i, j, kind, X)
                break
            span = slice(i + 1, j) if kind == 'pin' else slice(i + 1, j + 1)
            lo, hi = i, j
            if taken[lo:hi + 1].any():
                continue
            taken[lo:hi + 1] = True
            drop[span] = True
            if kind == 'twist':
                insert[i] = X
            removed += 1
        if keep_only is not None:
            i, j, kind, X = keep_only
            P = P[i:j + 1] if kind == 'pin_wrap' else np.vstack([[X], P[i + 1:j + 1]])
            removed += 1
            continue
        out = []
        for k in range(n):
            if not drop[k]:
                out.append(P[k])
            if k in insert:
                out.append(insert[k])
        P = np.asarray(out, float)
    return P, removed


# ---------------------------------------------------------------------------
# curve fitting

def douglas_peucker(pts, tol):
    """Indices of the vertices kept by Douglas-Peucker at `tol` (open
    polyline; the ends are always kept)."""
    pts = np.asarray(pts, float)
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = pts[b] - pts[a]
        L2 = float(seg @ seg)
        rel = pts[a + 1:b] - pts[a]
        # distance to the finite chord, not the infinite line: a hook that
        # doubles back along the chord's own line is 0 from the line and
        # must not be thinned away
        t = np.clip((rel @ seg) / L2, 0.0, 1.0) if L2 > 1e-24 else np.zeros(len(rel))
        dist = np.linalg.norm(rel - t[:, None] * seg, axis=1)
        k = int(np.argmax(dist))
        if dist[k] > tol:
            keep[a + 1 + k] = True
            stack.append((a, a + 1 + k))
            stack.append((a + 1 + k, b))
    return list(np.nonzero(keep)[0])


def _chord(p):
    return float(np.linalg.norm(np.asarray(p['p1'], float) - np.asarray(p['p0'], float)))


def _span_len(p):
    pts = p.get('_pts')
    if pts is None or len(pts) < 2:
        return _chord(p)
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


def _seg_dist_max(pts, a, b, chunk=256):
    """Worst distance from points (n, 2) to the segments a->b (m, 2),
    vectorised in chunks of points (n*m floats at a time)."""
    pts = np.asarray(pts, float)
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-18)
    worst = 0.0
    for i in range(0, len(pts), chunk):
        P = pts[i:i + chunk]
        rel = P[:, None, :] - a[None, :, :]                       # (c, m, 2)
        t = np.clip((rel * ab[None]).sum(2) / L2[None], 0.0, 1.0)  # (c, m)
        d = np.linalg.norm(rel - t[:, :, None] * ab[None], axis=2).min(1)
        worst = max(worst, float(d.max()) if len(d) else 0.0)
    return worst


def _open_polyline_dev(pts, q):
    """Worst distance from `pts` to the open polyline `q` (2-D)."""
    pts = np.asarray(pts, float)
    q = np.asarray(q, float)
    if len(q) < 2:
        return float(np.linalg.norm(pts - q[0], axis=1).max()) if len(q) else 0.0
    return _seg_dist_max(pts, q[:-1], q[1:])


def _prim_dev(p):
    """Worst distance of a primitive's own raw points from the finite
    primitive (segment or arc, not the infinite line or circle), so an
    endpoint that walked away from the data counts too."""
    pts = p.get('_pts')
    if pts is None or len(pts) < 2 or p['type'] == 'spline':
        return 0.0
    q = np.vstack([prim_points([p]), [np.asarray(p['p1'], float)]])
    return _open_polyline_dev(pts, q)


def _fragment_runs(prims, min_len, min_run, tol, closed=True):
    """Runs of primitives that lines and arcs did not really fit: a run of
    >= min_run consecutive crumbs shorter than min_len (a curve chopped
    into pieces), or any run holding a primitive whose own raw points sit
    more than tol off it (a stretch the segmenter gave up on). Returns a
    list of index lists, cyclic for a closed loop (a run may straddle the
    seam), in order for an open chain (a run may start at 0 or end at
    n - 1)."""
    n = len(prims)
    bad = [_prim_dev(p) > tol for p in prims]
    weak = [bad[i] or _span_len(p) < min_len for i, p in enumerate(prims)]
    if all(weak):
        return [list(range(n))] if (n >= min_run or any(bad)) else []
    if closed:
        # start after a strong primitive so runs do not straddle the seam
        start = next(i for i in range(n) if not weak[i])
        order = [(start + k) % n for k in range(1, n + 1)]
    else:
        order = list(range(n))
    runs, cur = [], []

    def flush():
        if len(cur) >= min_run or any(bad[j] for j in cur):
            runs.append(list(cur))
        cur.clear()

    for i in order:
        if weak[i]:
            cur.append(i)
        else:
            flush()
    flush()                     # an open chain may end in a run
    return runs


def _close_junctions(prims, closed=True):
    """Make consecutive primitives share their endpoint exactly: the two
    ends are averaged (they already sit within a hair of each other, both
    being the raw section point the segmenter split at). Arc endpoints
    are then put back on their circle and the neighbour follows, so a
    three-point arc drawn from p0, mid, p1 is the fitted circle. The two
    free ends of an open chain are left where they are."""
    from .profile_fit import _project_arc_ends
    n = len(prims)
    if n < 2:
        return prims
    pairs = range(n) if closed else range(n - 1)
    for i in pairs:
        a, b = prims[i], prims[(i + 1) % n]
        x = 0.5 * (np.asarray(a['p1'], float) + np.asarray(b['p0'], float))
        a['p1'] = x.copy()
        b['p0'] = x.copy()
        # a spline is drawn through its 'pts', so its ends must follow too
        # (a 0.05 mm gap here is invisible but leaves the sketch open)
        if a['type'] == 'spline':
            a['pts'][-1] = x
        if b['type'] == 'spline':
            b['pts'][0] = x
    for p in prims:
        if p['type'] == 'arc':
            _project_arc_ends(p)
    for i in pairs:
        a, b = prims[i], prims[(i + 1) % n]
        if a['type'] == 'arc':
            b['p0'] = np.asarray(a['p1'], float).copy()
            if b['type'] == 'spline':
                b['pts'][0] = b['p0']
        elif b['type'] == 'arc':
            a['p1'] = np.asarray(b['p0'], float).copy()
            if a['type'] == 'spline':
                a['pts'][-1] = a['p1']
    return prims


def _replace_with_splines(prims, runs, tol, closed=True):
    """Replace each fragment run by one spline primitive through the raw
    section points the fragments covered, thinned by Douglas-Peucker at
    tol/2. The spline starts and ends exactly on its neighbours' junction
    points, so a loop stays closed and an open chain keeps its ends."""
    if not runs:
        return prims
    n = len(prims)
    drop = set(i for r in runs for i in r)
    first_of = {r[0]: r for r in runs}
    out = []
    # closed: walk from the primitive after the last run's end, so every
    # run is met at its first index; open: runs are in order already
    start = (runs[-1][-1] + 1) % n if closed else 0
    for k in range(n):
        i = (start + k) % n
        if i in first_of:
            r = first_of[i]
            raw = [np.asarray(prims[j]['_pts'], float) for j in r]
            pts = [raw[0]] + [q[1:] for q in raw[1:]]
            pts = np.vstack(pts)
            pts = pts[douglas_peucker(pts, tol / 2)]
            p0 = np.asarray(prims[r[0]]['p0'], float)
            p1 = np.asarray(prims[r[-1]]['p1'], float)
            pts[0], pts[-1] = p0, p1
            out.append({'type': 'spline', 'pts': pts, 'p0': p0.copy(), 'p1': p1.copy(),
                        '_pts': np.vstack(raw)})
        elif i not in drop:
            out.append(prims[i])
    return out


def fit_loop(xy, tol=0.08, mesh_pts=None, min_prim_mm=None, min_run=3, clean=False,
             closed=True):
    """Redraw one 2-D loop (or, with closed=False, one open chain) as
    sketch primitives.

    Lines and arcs first (`profile_fit.segment_polyline`, the fitter the
    prismatic engine uses). Where that only manages a run of `min_run` or
    more crumbs shorter than `min_prim_mm` (default 8*tol, at least 0.6
    mm), or a primitive whose own points miss it by more than tol, the run
    becomes one {'type': 'spline', 'pts'} through the raw points, thinned
    at tol/2. Neighbours are then made to share their endpoints. A loop
    that is one circle within tol is returned as a full circle dict.

    An open chain skips the full-circle test, is segmented as an open
    polyline and keeps its two free ends exactly where the section put
    them; a whole-chain spline comes back with 'closed': False.

    `clean=True` also runs the prismatic engine's frame snapping and
    tangent junction solving (squared-up lines, true fillets). Off by
    default: on an organic section those moves cost more accuracy than
    they buy (measured on the RC-N2 controller: twice as many primitives
    off by more than tol). `mesh_pts` (optional, 2-D) refines arc fits on
    nearby mesh vertices, as the prismatic engine does.
    """
    from .profile_fit import refine_arcs_with_points
    pts = np.asarray(xy, float)
    if closed and len(pts) > 1 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    if len(pts) < (3 if closed else 2):
        return []
    if closed:
        circ = try_full_circle(pts, tol)
        if circ:
            if mesh_pts is not None:
                refine_arcs_with_points([circ], mesh_pts, tol)
            return circ
    prims = segment_polyline(pts, tol=tol, closed=closed)
    if not prims:
        return []
    if closed and len(prims) == 1 and prims[0]['type'] == 'arc':
        # one arc all the way round: start and end are the same point, and
        # Fusion's three-point arc refuses that ("Some input argument is
        # invalid", which ended a whole X-Ray). When the points really go
        # round it is a circle the circle test only just missed (a 0.34 mm
        # ring on the RC-N1 controller, 15 thinned points, 0.082 mm off
        # against tol 0.08): draw it as one. When they do not (a thin
        # sliver along a curved wall sits within tol of that wall's huge
        # circle: try_full_circle's min_cover rule) it is a closed spline
        # through its points, never a 100 mm disc.
        a = prims[0]
        ring = np.vstack([pts, pts[:1]])
        perim = float(np.linalg.norm(np.diff(ring, axis=0), axis=1).sum())
        if perim >= 0.8 * 2.0 * np.pi * float(a['r']):
            circ = {'type': 'arc', 'center': np.asarray(a['center'], float), 'r': float(a['r']),
                    'dev': float(a.get('dev', 0.0)), 'full': True, '_pts': pts}
            if mesh_pts is not None:
                refine_arcs_with_points([circ], mesh_pts, tol)
            return circ
        keep = douglas_peucker(ring, tol / 2)[:-1]
        if len(keep) < 3:
            keep = np.arange(len(pts))
        return [{'type': 'spline', 'pts': pts[keep], 'p0': pts[keep][0].copy(),
                 'p1': pts[keep][0].copy(), 'closed': True, '_pts': pts}]
    if mesh_pts is not None:
        refine_arcs_with_points(prims, mesh_pts, tol)
    if clean:
        prims = snap_profile(prims)
        solve_junctions(prims, closed=closed)
    if min_prim_mm is None:
        min_prim_mm = max(0.6, 8.0 * tol)
    runs = _fragment_runs(prims, min_prim_mm, min_run, tol, closed)
    if runs and len(runs) == 1 and len(runs[0]) == len(prims):
        # the whole loop is one curve: a single spline
        if closed:
            raw = np.vstack([np.asarray(p['_pts'], float)[:-1] for p in prims])
            keep = douglas_peucker(np.vstack([raw, raw[:1]]), tol / 2)[:-1]
            return [{'type': 'spline', 'pts': raw[keep], 'p0': raw[keep][0].copy(),
                     'p1': raw[keep][0].copy(), 'closed': True, '_pts': raw}]
        raw = np.vstack([np.asarray(prims[0]['_pts'], float)]
                        + [np.asarray(p['_pts'], float)[1:] for p in prims[1:]])
        q = raw[douglas_peucker(raw, tol / 2)]
        return [{'type': 'spline', 'pts': q, 'p0': q[0].copy(), 'p1': q[-1].copy(),
                 'closed': False, '_pts': raw}]
    prims = _close_junctions(_replace_with_splines(prims, runs, tol, closed), closed)
    return _uncross(prims, tol, closed)


def drawn_ends(p):
    """The two end points a primitive is actually drawn with: a spline
    through its 'pts', a line or arc from p0 to p1."""
    if p['type'] == 'spline':
        return np.asarray(p['pts'][0], float), np.asarray(p['pts'][-1], float)
    return np.asarray(p['p0'], float), np.asarray(p['p1'], float)


def junction_gap(prims, closed=True):
    """Largest distance between the drawn end of one primitive and the
    drawn start of the next (the closing pair included for a loop). Zero
    means every curve of the sketch meets its neighbour exactly, which is
    what Fusion needs to see a closed profile."""
    if isinstance(prims, dict) or len(prims) < 2:
        return 0.0
    n = len(prims)
    worst = 0.0
    for i in range(n if closed else n - 1):
        worst = max(worst, float(np.linalg.norm(drawn_ends(prims[i])[1]
                                                - drawn_ends(prims[(i + 1) % n])[0])))
    return worst


def _uncross(prims, tol, closed=True, max_rounds=8):
    """Where two fitted primitives cross each other (a tiny arc hooking past
    its neighbour at a sharp lip: the raw points never cross, the fit
    does), redraw the stretch from the first to the second as straight
    lines through their raw points, thinned at tol/2. Fusion refuses to
    loft a profile that crosses itself, however small the curl."""
    for _ in range(max_rounds):
        n = len(prims)
        if n < 2:
            return prims
        dense, owner = [], []
        for i, p in enumerate(prims):
            q = prim_points([p], arc_step_deg=1.0)
            dense.append(q)
            owner += [i] * len(q)
        if not closed:
            dense.append(np.asarray([prims[-1]['p1']], float))
            owner.append(n - 1)
        dense = np.vstack(dense)
        owner = np.asarray(owner)
        X = _crossings(dense) if closed else _crossings_open(dense)
        if not X:
            return prims
        i, j = int(owner[X[0][0]]), int(owner[X[0][1]])
        if i == j:
            return prims                          # a primitive crossing itself: leave it
        # the shorter way round from i to j
        fwd = (j - i) % n
        if closed and fwd > n - fwd:
            i, j = j, i
            fwd = (j - i) % n
        run = [(i + k) % n for k in range(fwd + 1)]
        if any(prims[k].get('_pts') is None for k in run):
            return prims
        raw = [np.asarray(prims[run[0]]['_pts'], float)] + \
              [np.asarray(prims[k]['_pts'], float)[1:] for k in run[1:]]
        raw = np.vstack(raw)
        raw[0] = prims[run[0]]['p0']
        raw[-1] = prims[run[-1]]['p1']
        if len(_crossings_open(raw)):
            return prims                          # the raw points cross too: trim's job, not ours
        kidx = douglas_peucker(raw, tol / 2)
        keep = raw[kidx]
        # each line owns only its own stretch of raw points (never the
        # whole run: that compounds round after round)
        lines = [{'type': 'line', 'p0': keep[k].copy(), 'p1': keep[k + 1].copy(),
                  '_pts': raw[kidx[k]:kidx[k + 1] + 1]} for k in range(len(keep) - 1)]
        drop = set(run)
        out = []
        for k in range(n):
            if k == run[0]:
                out.extend(lines)
            elif k not in drop:
                out.append(prims[k])
        prims = out
    return prims


def _crossings_open(P):
    """Proper crossings of an open polyline (no closing edge)."""
    n = len(P)
    if n < 4:
        return []
    Q = np.vstack([P, [P[0] + 1e9]])          # a far-off closing edge crosses nothing
    return [c for c in _crossings(Q) if c[0] < n - 1 and c[1] < n - 1]


def prim_points(prims, arc_step_deg=6.0, closed=True):
    """Dense 2-D polyline of a fitted loop (for deviation checks and
    previews). Full circles and closed splines come back closed; a loop's
    polyline implies its closing edge, an open chain (closed=False) ends
    on its last primitive's end point."""
    if isinstance(prims, dict):                       # full circle
        c, r = np.asarray(prims['center'], float), float(prims['r'])
        a = np.linspace(0, 2 * np.pi, 96, endpoint=False)
        return c + r * np.stack([np.cos(a), np.sin(a)], 1)
    out = []
    for p in prims:
        if p['type'] == 'line':
            out.append(np.asarray([p['p0']], float))
        elif p['type'] == 'arc':
            c, r = np.asarray(p['center'], float), float(p['r'])
            a0 = np.arctan2(p['p0'][1] - c[1], p['p0'][0] - c[0])
            a1 = np.arctan2(p['p1'][1] - c[1], p['p1'][0] - c[0])
            if p.get('ccw', True):
                while a1 <= a0:
                    a1 += 2 * np.pi
            else:
                while a1 >= a0:
                    a1 -= 2 * np.pi
            k = max(2, int(abs(a1 - a0) / np.radians(arc_step_deg)) + 1)
            a = np.linspace(a0, a1, k, endpoint=False)
            out.append(c + r * np.stack([np.cos(a), np.sin(a)], 1))
        else:
            out.append(np.asarray(p['pts'], float)[:-1] if not p.get('closed')
                       else np.asarray(p['pts'], float))
    if not closed and out:
        out.append(np.asarray([prims[-1]['p1']], float))
    return np.vstack(out) if out else np.zeros((0, 2))


def polyline_deviation(pts, ref):
    """Max distance from points `pts` to the closed polyline `ref` (2-D)."""
    ref = np.asarray(ref, float)
    return _seg_dist_max(pts, ref, np.roll(ref, -1, axis=0))


def _span(xy):
    xy = np.asarray(xy, float)
    return float(np.linalg.norm(xy.max(0) - xy.min(0))) if len(xy) else 0.0


def _touches(small, big, gap):
    """True when loop `small` crosses loop `big` (points on both sides) or
    comes within `gap` of its outline. Within the gap is enough: both are
    fitted afterwards, each free to stray by the tolerance, and a speck
    that only touched the raw outline crosses the fitted one."""
    small, big = np.asarray(small, float), np.asarray(big, float)
    if len(small) < 3 or len(big) < 3:
        return False
    lo, hi = small.min(0) - gap, small.max(0) + gap
    if np.any(hi < big.min(0)) or np.any(lo > big.max(0)):
        return False
    a, b = big, np.roll(big, -1, axis=0)
    near = ~(np.any(np.maximum(a, b) < lo, axis=1) | np.any(np.minimum(a, b) > hi, axis=1))
    if not near.any():
        return False
    a, b = a[near], b[near]
    d = b - a
    L2 = np.maximum(np.einsum('ij,ij->i', d, d), 1e-30)
    t = np.clip(np.einsum('kij,ij->ki', small[:, None, :] - a[None], d) / L2, 0.0, 1.0)
    dist = np.linalg.norm(small[:, None, :] - (a[None] + t[..., None] * d[None]), axis=2).min()
    if dist <= gap:
        return True
    ins = [point_in_loop(q, big) for q in small]
    return any(ins) and not all(ins)


def drop_crossing_specks(loops, size_mm, gap):
    """Drop the specks, loops no bigger than `size_mm` across (or with
    less area than a quarter of size_mm squared: a thin sliver), whose
    outline crosses a bigger loop's or comes within `gap` of it: an
    outer's, or a hole's (a speck straddling a hole's wall chops that
    hole's profile the same way).

    A textured surface (the RC-N1 controller's grip: 0.3 mm bumps)
    cuts into dozens of such specks sitting across the main outline. In
    a Fusion sketch every one of them chops the outline into extra
    profiles, and five slices of an X-Ray in a row lost their slabs to
    it, a 10 mm groove round the part. A speck that size is under the
    fit tolerance anyway. Specks wholly inside or outside are left alone.
    Returns (loops, specks dropped)."""
    def speck(xy):
        return _span(xy) <= size_mm or abs(signed_area(np.asarray(xy, float))) <= 0.25 * size_mm ** 2

    # the outlines a speck can chop: every outer and hole that is not a
    # speck itself (two specks touching are harmless, and testing every
    # pair was quadratic: 6 s a slice at 2000 specks)
    walls = [(i, r) for i, (o, h) in enumerate(loops) for r in [o] + list(h) if not speck(r)]

    def chops(r):
        sp = _span(r)
        return any(_span(w) > sp and _touches(r, w, gap) for _, w in walls)

    keep, dropped = [], 0
    for i, (outer, holes) in enumerate(loops):
        if speck(outer) and chops(outer):
            dropped += 1 + len(holes)
            continue
        h2 = [h for h in holes if not (speck(h) and chops(h))]
        dropped += len(holes) - len(h2)
        keep.append((outer, h2))
    return keep, dropped


def outer_only(loops):
    """Of nested loops [(outer, holes)], keep the outers that sit inside
    no other outer, and drop every hole. Returns (loops, inner dropped)."""
    keep, inner = [], 0
    areas = [abs(signed_area(o)) for o, _ in loops]
    for i, (outer, holes) in enumerate(loops):
        # only a bigger loop can contain this one: the even-odd test on a
        # raw loop that still crosses itself can say otherwise
        inside = any(j != i and areas[j] > areas[i] and point_in_loop(outer[0], loops[j][0])
                     for j in range(len(loops)))
        inner += len(holes) + (1 if inside else 0)
        if not inside:
            keep.append((outer, []))
    return keep, inner


def fit_section(V, F, origin, normal, tol=0.08, mesh_pts=None, join_mm=0.0, trim_mm=0.0,
                closed_only=False):
    """Cut and fit in one go. Returns {'origin', 'u', 'v', 'normal',
    'loops': [(outer_prims, [hole_prims...]), ...], 'open': [prims, ...]
    (open chains, fitted open), 'raw': the raw loops, 'raw_open': the raw
    open chains, 'stats': {'loops', 'holes', 'open', 'lines', 'arcs',
    'circles', 'splines', 'joins', 'max_gap', 'dev_max'}} where dev_max is
    the worst distance from the raw section points to the fitted curves.
    Free chain ends within `join_mm` are joined before fitting; with
    `trim_mm` > 0 the closed loops have their slivers thinner than that
    cut out first (`trim_slivers`), counted in stats['trimmed'].
    `closed_only` keeps just the outer outlines: inner loops and open
    chains are counted (stats['inner'], stats['open']) but neither
    trimmed nor fitted, which is most of the work on a busy section.
    Specks (loops under max(0.6, 8*tol) mm across) that cross a bigger
    outline or come within 2*tol of it are dropped (`drop_crossing_specks`), counted in
    stats['specks']."""
    loops, opens, frame, jst = section_curves(V, F, origin, normal, join_mm=join_mm)
    fitted, stats = [], dict(loops=0, holes=0, open=0, inner=0, lines=0, arcs=0, circles=0,
                             splines=0, joins=jst['joins'], max_gap=jst['max_gap'],
                             dev_max=0.0, junction_gap=0.0, trimmed=0, specks=0,
                             open_len=0.0, loop_len=0.0, open_max=0.0, loop_max=0.0,
                             close_gap=0.0)
    # With Outline only an outline left open is dropped, and an X-Ray slab
    # with it (the RC-N1 controller's vent grate: mesh holes up to 34 mm
    # at the slots, whole outlines gone from X-Ray slices). When most of
    # the cut is open, close the outline across the holes; say so.
    stats.update(bridged=0, bridge_max=0.0)
    open_len = sum(_length(c, False) for c in opens)
    # the dominant feature decides, not the totals: ten screw bosses inside
    # an open shell add up to more closed length than the shell's outline
    open_max = max((_length(c, False) for c in opens), default=0.0)
    loop_max = max((_length(o, True) for o, _ in loops), default=0.0)
    if closed_only and opens and open_max > loop_max:
        made, bridges = close_open_outline(opens, gap=max(join_mm, 2.0 * tol),
                                           slack=max(0.5, 4.0 * tol))
        if made:
            rings = [r for o, h in loops for r in [o] + list(h)] + made
            loops = nest_loops(rings)
            # what is left open are the scraps _big_chains passed over
            opens = [c for c in opens if _length(c, False) < 0.05 * open_len]
            stats['bridged'] = len(bridges)
            stats['bridge_max'] = max(bridges)
    # how much of the cut is still open, and what Join gaps would close
    # it (open_outline_note warns when that loses a slice)
    stats['open_len'] = sum(_length(c, False) for c in opens)
    stats['loop_len'] = sum(_length(o, True) for o, _ in loops)
    stats['open_max'] = max((_length(c, False) for c in opens), default=0.0)
    stats['loop_max'] = max((_length(o, True) for o, _ in loops), default=0.0)
    if opens:
        stats['close_gap'] = closing_gap(opens)
    if closed_only:
        stats['open'] = len(opens)
        opens = []
    if trim_mm > 0:
        trimmed = []
        for outer, holes in loops:
            o2, k = trim_slivers(outer, trim_mm)
            stats['trimmed'] += k
            h2 = []
            for h in holes:
                q, k = trim_slivers(h, trim_mm)
                stats['trimmed'] += k
                h2.append(q)
            trimmed.append((o2, h2))
        loops = trimmed
    # loops smaller than the shortest primitive the fit draws (see
    # fit_loop's min_prim_mm) that cross a bigger outline
    loops, stats['specks'] = drop_crossing_specks(loops, max(0.6, 8.0 * tol), 2.0 * tol)
    if closed_only:
        loops, stats['inner'] = outer_only(loops)

    def tally(prims, raw, closed):
        if isinstance(prims, dict):
            stats['circles'] += 1
        else:
            for p in prims:
                stats[{'line': 'lines', 'arc': 'arcs', 'spline': 'splines'}[p['type']]] += 1
            stats['junction_gap'] = max(stats['junction_gap'], junction_gap(prims, closed))
        if len(prims):
            sub = raw[::max(1, len(raw) // 400)]
            dev = (polyline_deviation(sub, prim_points(prims)) if closed
                   else _open_polyline_dev(sub, prim_points(prims, closed=False)))
            stats['dev_max'] = max(stats['dev_max'], dev)

    for outer, holes in loops:
        fo = fit_loop(outer, tol, mesh_pts)
        fh = [fit_loop(h, tol, mesh_pts) for h in holes]
        fitted.append((fo, fh))
        stats['loops'] += 1
        stats['holes'] += len(holes)
        for prims, raw in [(fo, outer)] + list(zip(fh, holes)):
            tally(prims, raw, True)
    fopen = []
    for xy in opens:
        prims = fit_loop(xy, tol, mesh_pts, closed=False)
        fopen.append(prims)
        stats['open'] += 1
        tally(prims, xy, False)
    o, u, v, n = frame
    return {'origin': o, 'u': u, 'v': v, 'normal': n, 'loops': fitted, 'open': fopen,
            'raw': loops, 'raw_open': opens, 'stats': stats}


def lift_prims(prims, frame):
    """Fitted 2-D primitives of one loop -> the same primitives with 3-D
    points (mm) in `frame` = (origin, u, v, n): 'p0', 'p1', 'mid' (arcs),
    'pts' (splines), or {'center3', 'r'} for a full circle. What the Fusion
    sections script and the web preview draw."""
    def L(xy):
        return to_3d(np.asarray(xy, float).reshape(-1, 2), frame)
    if isinstance(prims, dict):
        return {'type': 'circle', 'center3': L(prims['center'])[0].tolist(), 'r': float(prims['r'])}
    out = []
    for p in prims:
        if p['type'] == 'line':
            out.append({'type': 'line', 'p0': L(p['p0'])[0].tolist(), 'p1': L(p['p1'])[0].tolist()})
        elif p['type'] == 'arc':
            out.append({'type': 'arc', 'p0': L(p['p0'])[0].tolist(), 'p1': L(p['p1'])[0].tolist(),
                        'mid': L(_arc_mid(p))[0].tolist(), 'r': float(p['r'])})
        else:
            pts = np.asarray(p['pts'], float)
            if p.get('closed'):
                pts = np.vstack([pts, pts[:1]])
            out.append({'type': 'spline', 'pts': L(pts).tolist()})
    return out


def section_preview(V, F, origin, normal, tol=0.08, join_mm=0.0, closed_only=False, trim_mm=0.0):
    """One traced section for the web app and the sections script:
    {'origin', 'normal', 'loops': [(outer3d, [holes3d])], 'open':
    [prims3d...] (open chains), 'polylines': [[x, y, z]...] dense curves
    per loop for drawing (closed ones repeat their first point, open ones
    do not), 'areas_mm2': the material area of each loop (outer minus its
    holes; what a Fusion profile of it measures, so a script can tell the
    material profiles from the hole interiors Fusion also lists),
    'stats'}. `closed_only` keeps just the outer outlines: the
    open chains (loose pieces of a leaky mesh) and every inner loop (a
    hole, an island inside a hole) are left out of the drawing, an outline
    to extrude and nothing else; stats['open'] and stats['inner'] still
    count them, so the caller can say how many were skipped."""
    sec = fit_section(V, F, origin, normal, tol=tol, join_mm=join_mm, trim_mm=trim_mm,
                      closed_only=closed_only)
    frame = (sec['origin'], sec['u'], sec['v'], sec['normal'])
    loops, polys, areas = [], [], []
    for fo, fh in sec['loops']:
        loops.append((lift_prims(fo, frame), [lift_prims(h, frame) for h in fh]))
        area = 0.0
        for k, prims in enumerate([fo] + fh):
            if len(prims):
                q = prim_points(prims)
                polys.append(to_3d(np.vstack([q, q[:1]]), frame).tolist())
                area += abs(signed_area(q)) * (1.0 if k == 0 else -1.0)
        areas.append(float(max(area, 0.0)))
    opens = []
    for prims in sec['open']:
        opens.append(lift_prims(prims, frame))
        if len(prims):
            polys.append(to_3d(prim_points(prims, closed=False), frame).tolist())
    return {'origin': np.asarray(sec['origin']).tolist(), 'normal': np.asarray(sec['normal']).tolist(),
            'loops': loops, 'open': opens, 'polylines': polys, 'areas_mm2': areas,
            'stats': sec['stats']}


# ---------------------------------------------------------------------------
# a stack of planes (x-ray)

# a plane is never put exactly on a flat end face of the part: the cut
# there is degenerate (every triangle of the face lies in the plane)
STACK_EDGE_MM = 1e-3


def stack_offsets(frm, to, step, half_extent, edge=STACK_EDGE_MM, tol=1e-9):
    """Plane offsets (mm from the bounding-box centre) of a stack from
    `frm` to `to` every `step`: frm + k*step for every k that stays at or
    under `to`, and `to` itself as the last plane when the last multiple
    falls short, so the end plane the user chose is always cut. Both ends
    are clamped to +-(half_extent - edge); frm > to is swapped; step <= 0
    or frm == to gives just the one plane."""
    lo, hi = sorted((float(frm), float(to)))
    lim = max(0.0, float(half_extent) - edge)
    lo, hi = min(max(lo, -lim), lim), min(max(hi, -lim), lim)
    if step <= 0 or hi - lo <= tol:
        return [lo]
    n = int(math.floor((hi - lo) / step + tol))
    out = [lo + k * step for k in range(n + 1)]     # k*step, never accumulated
    if hi - out[-1] > tol:
        out.append(hi)
    return out


def stack_extrudes(ats, step, far):
    """How far each slice of a stack is extruded to make solid slabs:
    `ats` are the planes' positions along the axis, ascending (mm), `far`
    the farthest a slab may reach (the part's far face). Every slice runs
    to the next plane; the last one a full `step`, but never past `far`.
    The end plane is always cut, so the slice before it may be a hair
    away: that end plane is then drawn only (None) and the previous slab
    runs through to where the end slab would have finished. An end plane
    sitting on the far face is drawn only too. Fewer than two planes:
    nothing is extruded."""
    n = len(ats)
    exts = [None] * n
    if n < 2:
        return exts
    for i in range(n - 1):
        exts[i] = ats[i + 1] - ats[i]
    exts[-1] = max(0.0, min(step, far - ats[-1]))
    if n > 2 and exts[-2] < 0.5 * step:
        exts[-2] += exts[-1]
        exts[-1] = None
    if exts[-1] is not None and exts[-1] < 0.01:
        exts[-1] = None
    return exts
