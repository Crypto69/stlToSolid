"""A picture of one traced section, so a model calling the MCP tools can see
the slice the way a person sees it in the web app's 3-D view. PIL only."""
import io
import math

_PX = 720
_MARGIN = 26
_TITLE = 30
_AXES = {'x': (1, 2, 'Y', 'Z'), 'y': (0, 2, 'X', 'Z'), 'z': (0, 1, 'X', 'Y')}


def _grid_step(extent):
    for g in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if extent / g <= 12:
            return g
    return 2000


def section_png(polylines, axis, at, px=_PX):
    """PNG bytes of a section's dense curves ([[x, y, z]...] per curve, mm)
    seen along `axis`, with a labelled millimetre grid. A closed curve is
    black and an open chain (a leak in the mesh) red, because an open chain
    is the first thing a reader of the picture needs to notice."""
    from PIL import Image, ImageDraw, ImageFont
    iu, iv, lu, lv = _AXES[axis]
    pts = [[(p[iu], p[iv]) for p in line] for line in polylines if line]
    im = Image.new('RGB', (px, px), 'white')
    dr = ImageDraw.Draw(im)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:                 # an older Pillow: the bitmap font
        font = ImageFont.load_default()
    head = f'{axis.upper()} = {at:.2f} mm, {lu} to the right, {lv} up'
    if not any(pts):
        dr.text((10, 8), head + ' (no material on this plane)', fill=(20, 20, 20), font=font)
    else:
        us = [p[0] for line in pts for p in line]
        vs = [p[1] for line in pts for p in line]
        lo_u, hi_u, lo_v, hi_v = min(us), max(us), min(vs), max(vs)
        span_u, span_v = max(hi_u - lo_u, 1e-6), max(hi_v - lo_v, 1e-6)
        s = min((px - 2 * _MARGIN) / span_u, (px - 2 * _MARGIN - _TITLE) / span_v)
        ox = (px - span_u * s) / 2.0
        oy = _TITLE + (px - _TITLE - span_v * s) / 2.0

        def at_px(u, v):
            return ox + (u - lo_u) * s, oy + (hi_v - v) * s

        g = _grid_step(max(span_u, span_v))
        for k in range(int(math.ceil(lo_u / g)), int(math.floor(hi_u / g)) + 1):
            x = at_px(k * g, 0)[0]
            dr.line([(x, oy), (x, oy + span_v * s)], fill=(225, 225, 225))
        for k in range(int(math.ceil(lo_v / g)), int(math.floor(hi_v / g)) + 1):
            y = at_px(0, k * g)[1]
            dr.line([(ox, y), (ox + span_u * s, y)], fill=(225, 225, 225))
        for line in pts:
            closed = len(line) > 2 and line[0] == line[-1]
            dr.line([at_px(*p) for p in line], fill=(20, 20, 20) if closed else (205, 40, 40), width=2)
        dr.text((10, 8), f'{head}  {span_u:.1f} x {span_v:.1f} mm  (grid {g:g} mm)',
                fill=(20, 20, 20), font=font)
    out = io.BytesIO()
    im.save(out, 'PNG', optimize=True)
    return out.getvalue()
