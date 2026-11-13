"""Volume sanity on Boolean operations.

OCC can hand back a Boolean that reads valid and is wrong: a cut that ate
the whole body (one hollow controller body came out with volume 0 after
its cavity cut), a fuse that lost a piece. Volumes give cheap invariants
that no correct result breaks:

  cut   A - B :  max(0, V(A) - V(B)) <= V(R) <= V(A)
  fuse  A + B :  max(V(A), V(B))     <= V(R) <= V(A) + V(B)

each with a slack for the fuzzy value and the volume integration. A result
that breaks one is retried with other fuzzy values, then refused with
BooleanGuardError, so the caller can skip that one feature or fall back to
another route instead of shipping a wrong solid.

`cq.Shape.Volume()` is used: exact on planes and cylinders, 1-2 % off on
B-spline faces, so callers with B-spline bodies (the sliced loft) pass a
wider `slack_pct`. The invariants are for gross failures, not for grading.
"""


class BooleanGuardError(RuntimeError):
    """A Boolean whose result volume no correct result could have."""


SLACK_PCT = 0.5          # % of the larger operand
SLACK_ABS = 1e-6         # mm^3, for tiny parts


def volume(x):
    """Volume of a CadQuery Workplane or Shape (0 for an empty result)."""
    shape = x.val() if hasattr(x, 'val') else x
    try:
        return abs(float(shape.Volume()))
    except Exception:
        return 0.0


def check(op, va, vb, vr, slack_pct=SLACK_PCT):
    """None if `vr` is a volume `op` ('cut' or 'fuse') of operands with
    volumes `va`, `vb` can give, else the broken invariant as text."""
    s = slack_pct / 100.0 * max(va, vb) + SLACK_ABS
    if op == 'cut':
        if vr > va + s:
            return f"cut gained volume ({va:.4g} -> {vr:.4g} mm3)"
        if vr < va - vb - s:
            return (f"cut removed more than the tool holds "
                    f"({va:.4g} - {vb:.4g} -> {vr:.4g} mm3)")
        return None
    if op == 'fuse':
        if vr < max(va, vb) - s:
            return f"fuse lost volume ({va:.4g} + {vb:.4g} -> {vr:.4g} mm3)"
        if vr > va + vb + s:
            return f"fuse made volume ({va:.4g} + {vb:.4g} -> {vr:.4g} mm3)"
        return None
    raise ValueError(op)


def _run(op, a, b, tol):
    if op == 'cut':
        return a.cut(b, tol=tol) if tol is not None else a.cut(b)
    if hasattr(a, 'union'):                       # Workplane
        return a.union(b, tol=tol) if tol is not None else a.union(b)
    return a.fuse(b, tol=tol) if tol is not None else a.fuse(b)


def guarded(op, a, b, tol=None, retry_tols=(None, 1e-4, 1e-3), slack_pct=SLACK_PCT):
    """`a` cut by / fused with `b` (CadQuery Workplanes or Shapes), the
    first result whose volume passes `check`: with `tol` first, then each
    of `retry_tols`. Raises BooleanGuardError when none does (the last
    reason in the message); kernel exceptions of every try are kept and
    re-raised as that error too."""
    va, vb = volume(a), volume(b)
    tries = [tol] + [t for t in retry_tols if t != tol]
    why = None
    for t in tries:
        try:
            r = _run(op, a, b, t)
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            continue
        why = check(op, va, vb, volume(r), slack_pct)
        if why is None:
            return r
    raise BooleanGuardError(why or f"{op} failed")


def guarded_cut(a, b, tol=None, **kw):
    return guarded('cut', a, b, tol, **kw)


def guarded_fuse(a, b, tol=None, **kw):
    return guarded('fuse', a, b, tol, **kw)
