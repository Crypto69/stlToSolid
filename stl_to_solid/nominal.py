"""Nominal values: a measured number snapped to the roundest value that the
measurement allows.

A designer types 3, 1.5 or 6.6, not 2.99987. A fitted value carries the
mesh's error, so it lands a hair off the number that was typed. The rule
(the one CMM software and ScanRuler use): snap to a nominal only when the
nominal lies within the tolerance of the measurement, never across it, and
keep the measured value beside it.

The tolerance comes from the mesh (`snap_tolerance`): three times the
noise on a scan, a micron-level floor on a CAD export, where vertices sit
exactly on the true surface and anything more than float noise is design.

numpy only, on purpose: the in-Fusion add-ins may copy it.
"""
import numpy as np

# Grids tried coarsest first: the first grid value within tolerance wins,
# so 2.98 with a 0.05 tolerance is 3, not 2.98's nearest 0.01.
STEPS = (1.0, 0.5, 0.1, 0.05, 0.01)
# A CAD export's vertices are exact to float32 (micron level on a part of
# a few hundred mm): a value this close to a grid value is that value.
SNAP_FLOOR = 1e-3


def snap_tolerance(noise, fit_tol):
    """How far a measured value may move to reach a nominal: three times
    the mesh noise, at least SNAP_FLOOR, never more than the fit tolerance
    (a snap must not move geometry further than the fit itself may)."""
    noise = float(noise) if noise is not None and np.isfinite(noise) else 0.0
    return float(min(max(3.0 * noise, SNAP_FLOOR), max(fit_tol, SNAP_FLOOR)))


def nominal(x, tol, steps=STEPS):
    """(value, step): the roundest grid value strictly within `tol` of `x`,
    with the grid step it sits on, or (x, None) when none is."""
    x = float(x)
    for s in steps:
        n = round(round(x / s) * s, 9)
        if abs(n - x) < tol:
            return n, s
    return x, None


def snap_radius(r, tol):
    """Radius snapped on the grids above (a fillet's R1.5, R0.5)."""
    v, _ = nominal(r, tol)
    return v if v > 0 else float(r)


def snap_diameter_radius(r, tol):
    """Radius whose DIAMETER is snapped: holes and full circles are
    designed by diameter (Ø3.4 clearance, Ø6), so the grid applies to 2r.
    The diameter may move twice as far as the radius tolerance."""
    d, _ = nominal(2.0 * float(r), 2.0 * tol)
    return d / 2.0 if d > 0 else float(r)
