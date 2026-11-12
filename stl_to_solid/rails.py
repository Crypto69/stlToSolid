"""Rails: sections at 90 degrees to an X-Ray stack that know the stack.

A loft through a stack of profiles follows them much better with rails,
but Fusion refuses a rail unless it touches every profile exactly. The
fitted profile curves drift up to the fit tolerance from the raw mesh cut,
so a raw 90-degree cut misses them by that much. Here every rail is a cut
of the mesh by a plane through the part's centre along the stack's axis,
and where it crosses each profile plane its point is replaced by the point
where that rail plane crosses the *fitted* profile curve (on a line or an
arc exactly; on a spline the crossing is added to the spline's fit points,
so the fitted spline Fusion draws passes through it).

numpy only, on purpose: the file is copied into the Mesh to Outline add-in
next to section_fit.py.
"""
import math

import numpy as np

from .section_fit import chain_loops, cut_segments, douglas_peucker, plane_basis


# a rail point further than this from every profile crossing of its plane
# (mm, plus 3*tol) is not that profile's crossing
SNAP_EXTRA_MM = 0.05
_EPS = 1e-9


def rail_planes(axis_normal, centre, count=2):
    """[(origin, normal)] of `count` rail planes through `centre`, all
    containing the stack's axis: count 2 gives the planes square to u and
    to v of plane_basis(axis_normal) (the stack's own sketch frame, so for
    an axis along X, Y or Z both are axis-aligned too); more spread the
    normals evenly over 180 degrees."""
    u, v, _n = plane_basis(axis_normal)
    c = np.asarray(centre, float)
    out = []
    for k in range(int(count)):
        a = math.pi * k / count
        m = math.cos(a) * u + math.sin(a) * v
        m /= np.linalg.norm(m)
        out.append((c.copy(), m))
    return out


def _arc_contains(p, ang):
    """True when angle `ang` (radians, about the arc's centre) lies on the
    arc's sweep from p0 to p1 (its own direction, ccw by default)."""
    c = np.asarray(p['center'], float)
    a0 = math.atan2(p['p0'][1] - c[1], p['p0'][0] - c[0])
    a1 = math.atan2(p['p1'][1] - c[1], p['p1'][0] - c[0])
    if p.get('ccw', True):
        sweep = (a1 - a0) % (2 * math.pi)
        rel = (ang - a0) % (2 * math.pi)
    else:
        sweep = (a0 - a1) % (2 * math.pi)
        rel = (a0 - ang) % (2 * math.pi)
    if sweep < _EPS:
        sweep = 2 * math.pi
    return rel <= sweep + 1e-12


def _circle_line(c, r, a, b, k):
    """Points of the circle (c, r) on the line a*x + b*y = k (2-D)."""
    nrm = np.array([a, b], float)
    L = float(np.linalg.norm(nrm))
    if L < _EPS:
        return []
    nrm /= L
    d = (k / L) - float(nrm @ c)                  # signed distance centre -> line
    if abs(d) > r + 1e-12:
        return []
    foot = c + d * nrm
    h = math.sqrt(max(r * r - d * d, 0.0))
    t = np.array([-nrm[1], nrm[0]])
    if h < 1e-12:
        return [foot]
    return [foot + h * t, foot - h * t]


def _line_hits(p0, p1, a, b, k):
    f0 = a * p0[0] + b * p0[1] - k
    f1 = a * p1[0] + b * p1[1] - k
    if f0 == 0 and f1 == 0:
        return []                                  # the segment lies on the rail plane
    if (f0 > 0 and f1 > 0) or (f0 < 0 and f1 < 0):
        return []
    t = f0 / (f0 - f1)
    return [np.asarray(p0, float) + t * (np.asarray(p1, float) - np.asarray(p0, float))]


def rail_crossings(sec2d, rail_origin, rail_normal):
    """Where the rail plane (rail_origin, rail_normal) crosses the fitted
    outer loops of one traced section (section_fit.fit_section's result,
    2-D primitives): returns a list of 3-D points (mm) that lie on the
    drawn profile curves by construction. A crossing inside a spline is
    inserted into that spline's fit points (`pts`, in place), so the
    spline Fusion fits passes through it. Holes are left out."""
    o = np.asarray(sec2d['origin'], float)
    u = np.asarray(sec2d['u'], float)
    v = np.asarray(sec2d['v'], float)
    m = np.asarray(rail_normal, float)
    a, b = float(u @ m), float(v @ m)
    k = float((np.asarray(rail_origin, float) - o) @ m)
    if math.hypot(a, b) < 1e-9:
        return []                                  # the rail plane is the profile plane
    hits = []
    for outer, _holes in sec2d['loops']:
        if isinstance(outer, dict):                # a full circle
            hits += _circle_line(np.asarray(outer['center'], float), float(outer['r']), a, b, k)
            continue
        for p in outer:
            if p['type'] == 'line':
                hits += _line_hits(p['p0'], p['p1'], a, b, k)
            elif p['type'] == 'arc':
                hits += _drawn_arc_hits(p, a, b, k)
            else:
                hits += _spline_hits(p, a, b, k)
    # a crossing at a junction is found on both curves
    out = []
    for q in hits:
        if all(np.linalg.norm(q - w) > 1e-7 for w in out):
            out.append(np.asarray(q, float))
    return [o + q[0] * u + q[1] * v for q in out]


def _drawn_arc_hits(p, a, b, k):
    """Crossings with the arc as Fusion draws it: the three-point arc
    through p0, its mid point and p1 (an end pinned to a line can sit a
    little off the fitted circle, and the drawn arc follows the ends)."""
    from .profile_fit import _arc_mid
    p0 = np.asarray(p['p0'], float)
    p1 = np.asarray(p['p1'], float)
    pm = np.asarray(_arc_mid(p), float)
    d1, d2 = pm - p0, p1 - p0
    den = 2.0 * (d1[0] * d2[1] - d1[1] * d2[0])
    if abs(den) < 1e-12:                           # collinear: drawn as two lines
        return _line_hits(p0, pm, a, b, k) + _line_hits(pm, p1, a, b, k)
    s1, s2 = d1 @ d1, d2 @ d2
    c = p0 + np.array([d2[1] * s1 - d1[1] * s2, d1[0] * s2 - d2[0] * s1]) / den
    r = float(np.linalg.norm(p0 - c))
    ccw = (d1[0] * (p1 - pm)[1] - d1[1] * (p1 - pm)[0]) > 0
    drawn = {'center': c, 'p0': p0, 'p1': p1, 'ccw': ccw}
    return [q for q in _circle_line(c, r, a, b, k)
            if _arc_contains(drawn, math.atan2(q[1] - c[1], q[0] - c[0]))]


def _spline_hits(p, a, b, k):
    pts = np.asarray(p['pts'], float)
    ring = np.vstack([pts, pts[:1]]) if p.get('closed') else pts
    f = ring @ np.array([a, b]) - k
    hits, inserts = [], []
    for i in range(len(ring) - 1):
        if f[i] == 0:
            hits.append(ring[i].copy())
            continue
        if (f[i] > 0) == (f[i + 1] > 0) or f[i + 1] == 0:
            continue
        t = f[i] / (f[i] - f[i + 1])
        q = ring[i] + t * (ring[i + 1] - ring[i])
        hits.append(q)
        if (np.linalg.norm(q - ring[i]) > 1e-7 and np.linalg.norm(q - ring[i + 1]) > 1e-7):
            inserts.append((i + 1, q))
    if not p.get('closed') and len(f) and f[-1] == 0:
        hits.append(ring[-1].copy())
    if inserts:
        new = list(pts)
        for at, q in reversed(inserts):
            new.insert(at, q)                      # at == len(pts) is the closing edge: append
        p['pts'] = np.asarray(new, float)
    return hits


def _pieces(P, closed, h, lo, hi):
    """Split a cut chain (3-D points P, axis coordinates h) into the open
    pieces between axis positions lo and hi, segment by segment (an edge
    may run straight across the whole span), each piece ending exactly at
    the boundary it leaves by. Returns a list of (points, h)."""
    P = np.asarray(P, float)
    h = np.asarray(h, float)
    n = len(P)
    if closed:
        out_idx = np.flatnonzero((h < lo - _EPS) | (h > hi + _EPS))
        if not len(out_idx):
            # the whole loop lies between the first and last profile
            return [np.vstack([P, P[:1]])]
        s = int(out_idx[0])
        order = [(s + i) % n for i in range(n)] + [s]
        P, h = P[order], h[order]
    pieces, cur = [], None
    for i in range(len(P) - 1):
        a, b = h[i], h[i + 1]
        if abs(b - a) < _EPS:
            span = (0.0, 1.0) if lo - _EPS <= a <= hi + _EPS else None
        else:
            ta, tb = (lo - a) / (b - a), (hi - a) / (b - a)
            t0, t1 = max(0.0, min(ta, tb)), min(1.0, max(ta, tb))
            span = (t0, t1) if t0 <= t1 else None
        if span is None:
            if cur is not None:
                pieces.append(cur)
                cur = None
            continue
        t0, t1 = span
        q0 = P[i] + t0 * (P[i + 1] - P[i])
        q1 = P[i] + t1 * (P[i + 1] - P[i])
        if cur is not None and t0 <= 1e-12:
            cur.append(q1)
        else:
            if cur is not None:
                pieces.append(cur)
            cur = [q0, q1]
        if t1 < 1.0 - 1e-12:
            pieces.append(cur)
            cur = None
    if cur is not None:
        pieces.append(cur)
    n_hat_h = []
    for q in pieces:
        q = np.asarray(q, float)
        keep = np.ones(len(q), bool)
        keep[1:] = np.linalg.norm(np.diff(q, axis=0), axis=1) > 1e-9
        q = q[keep]
        if len(q) >= 2:
            n_hat_h.append(q)
    return n_hat_h


def _monotonic(pieces, n_hat, back):
    """Split every piece where it turns back along the axis by more than
    `back` mm: a cut of a shell runs out along the outer skin and back
    along the inner one, and each way is a rail of its own (a rail crosses
    each profile once). Smaller wiggles of a scanned surface stay in the run
    (a profile crossed twice there is flagged, not split). The split is at
    the turning point, which both runs keep."""
    out = []
    for q in pieces:
        h = q @ n_hat
        start, ext, d = 0, 0, 0        # run start, index of its extreme, direction (+1/-1, 0 unknown)
        for i in range(1, len(h)):
            if d == 0:
                if abs(h[i] - h[start]) > back:
                    d = 1 if h[i] > h[start] else -1
                    ext = i
                elif (h[i] - h[start]) * (h[ext] - h[start]) >= 0 and abs(h[i] - h[start]) >= abs(h[ext] - h[start]):
                    ext = i
                continue
            if d * (h[i] - h[ext]) > 0:
                ext = i
            elif d * (h[ext] - h[i]) > back:
                out.append(q[start:ext + 1])
                start, d = ext, -d
                # the new run's extreme so far
                seg = h[start:i + 1]
                ext = start + int(np.argmax(d * seg))
        out.append(q[start:])
    return [q for q in out if len(q) >= 2]


def _crossings_at(h, target):
    """Positions (fractional index) where the polyline's axis coordinate
    `h` passes `target`. Vertices exactly on it (a run of them) count once."""
    d = np.asarray(h, float) - target
    n = len(d)
    out, i = [], 0
    while i < n:
        if abs(d[i]) <= _EPS:
            j = i
            while j + 1 < n and abs(d[j + 1]) <= _EPS:
                j += 1
            out.append(float(i))
            i = j + 1
            continue
        if i + 1 < n and abs(d[i + 1]) > _EPS and (d[i] > 0) != (d[i + 1] > 0):
            out.append(i + d[i] / (d[i] - d[i + 1]))
        i += 1
    return out


def _at(P, x):
    i = int(math.floor(x))
    if i >= len(P) - 1:
        return P[-1].copy()
    t = x - i
    return P[i] + t * (P[i + 1] - P[i])


def trace_rails(V, F, axis_normal, centre, secs2d, tol=0.08, count=2):
    """Rails for a stack of traced sections `secs2d` (fit_section results,
    in stack order, every one square to `axis_normal`).

    For each of `count` rail planes (rail_planes through `centre`): the raw
    mesh cut, clipped to the span from the first profile plane to the last,
    one rail per piece. A piece that crosses every profile plane exactly
    once, each time within 3*tol + 0.05 mm of where the rail plane crosses
    that profile's fitted outline, is 'complete': its crossings are moved
    onto the profile curves and forced into its fit points (the raw points
    between them thinned at tol/2), its ends are the first and last
    profiles' crossings. Anything else comes back with complete False and
    a note (drawn, but no loft should use it). A cut that turns back along
    the axis (a shell's outer and inner skin) is split there into one rail
    each way; pieces that do not reach two profile planes are dropped.

    Returns [{'plane': {'origin', 'normal'}, 'pts': [[x, y, z]...],
    'complete': bool, 'crossings': [[profile index, [x, y, z]]...],
    'note': str}]."""
    n_hat = np.asarray(axis_normal, float)
    n_hat = n_hat / np.linalg.norm(n_hat)
    if len(secs2d) < 2:
        return []
    heights = [float(np.asarray(s['origin'], float) @ n_hat) for s in secs2d]
    order = np.argsort(heights)
    if list(order) != list(range(len(heights))) and list(order) != list(range(len(heights)))[::-1]:
        raise ValueError('the sections are not in stack order')
    lo, hi = min(heights), max(heights)
    snap = 3.0 * tol + SNAP_EXTRA_MM
    # a turn back along the axis bigger than this splits a cut into two rails
    gaps = np.diff(sorted(heights))
    back = max(10.0 * tol, 0.25 * float(gaps.min())) if len(gaps) else 10.0 * tol
    rails = []
    for ro, rm in rail_planes(n_hat, centre, count):
        prof_x = [rail_crossings(s, ro, rm) for s in secs2d]
        P, S = cut_segments(V, F, ro, rm)
        for idx, closed in chain_loops(S):
            Q = P[idx]
            keep = np.ones(len(Q), bool)
            keep[1:] = np.linalg.norm(np.diff(Q, axis=0), axis=1) > 1e-9
            Q = Q[keep]
            if len(Q) < 2:
                continue
            for piece in _monotonic(_pieces(Q, closed, Q @ n_hat, lo, hi), n_hat, back):
                h = piece @ n_hat
                h[np.abs(h - lo) <= 1e-7] = lo         # the clipped ends sit on the end planes
                h[np.abs(h - hi) <= 1e-7] = hi
                if h[0] > h[-1]:
                    piece, h = piece[::-1], h[::-1]
                # a piece between two neighbouring profile planes is no rail
                if sum(1 for hk in heights if h.min() - _EPS <= hk <= h.max() + _EPS) < 2:
                    continue
                rails.append(_one_rail(piece, h, heights, prof_x, ro, rm, tol, snap))
    return rails


def _one_rail(piece, h, heights, prof_x, ro, rm, tol, snap):
    plane = {'origin': np.asarray(ro, float).tolist(), 'normal': np.asarray(rm, float).tolist()}
    notes, where, crossings = [], [], []
    for k, hk in enumerate(heights):
        xs = _crossings_at(h, hk)
        if len(xs) != 1:
            notes.append('section %d: %s' % (k + 1, 'missed' if not xs else 'crossed %d times' % len(xs)))
            continue
        raw = _at(piece, xs[0])
        cands = prof_x[k]
        if not cands:
            notes.append('section %d: its outline does not reach the rail plane' % (k + 1))
            continue
        d = [float(np.linalg.norm(raw - c)) for c in cands]
        j = int(np.argmin(d))
        if d[j] > snap:
            notes.append('section %d: the outline is %.2f mm from the rail' % (k + 1, d[j]))
            continue
        where.append(xs[0])
        crossings.append([k, np.asarray(cands[j], float)])
    complete = not notes and len(crossings) == len(heights)
    if not complete:
        pts = piece[douglas_peucker(piece, tol / 2.0)]
        return {'plane': plane, 'pts': pts.tolist(), 'complete': False,
                'crossings': [[k, q.tolist()] for k, q in crossings],
                'note': '; '.join(notes[:4]) + (' ...' if len(notes) > 4 else '')}
    # stitch: crossing, thinned raw points, crossing, ... (the first and
    # last crossings are the rail's ends)
    out = [crossings[0][1]]
    for (x0, (_, c0)), (x1, (_, c1)) in zip(zip(where, crossings), zip(where[1:], crossings[1:])):
        i0, i1 = int(math.floor(x0)) + 1, int(math.ceil(x1))
        mid = piece[i0:i1]
        if len(mid):
            far = (np.linalg.norm(mid - c0, axis=1) > 1e-7) & (np.linalg.norm(mid - c1, axis=1) > 1e-7)
            mid = mid[far]
        seg = np.vstack([c0[None], mid, c1[None]])
        kept = seg[douglas_peucker(seg, tol / 2.0)]
        out.extend(kept[1:])
    pts = np.asarray(out, float)
    return {'plane': plane, 'pts': pts.tolist(), 'complete': True,
            'crossings': [[k, q.tolist()] for k, q in crossings], 'note': ''}
