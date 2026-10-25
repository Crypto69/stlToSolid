"""Pictures of a built part, for the model to compare with the drawing and
for the panel to show what was checked: front, top, right-side and
isometric views of a mesh, each titled with the view, its extent in mm
and the grid size.

numpy + PIL only (no matplotlib, which the desktop bundle may not carry).
A painter's algorithm per view: triangles sorted far to near, back faces
dropped, each filled with a grey from its slope to the light and, in a
second buffer, with its own index. Crease and silhouette edges are then
inked only where that index buffer still shows one of the edge's two
faces, sampled every couple of pixels along the edge: hidden lines stay
hidden, visible ones come out full strength, exact up to the depth sort.
Drawn at 2x and downsampled, so the lines are smooth. A few thousand
triangles take well under a second.
"""
import io
import math

import numpy as np

# view -> the direction from the part toward the viewer, and the caption
VIEWS = {
    'front': ((0.0, -1.0, 0.0), 'FRONT view (looking along +Y; X to the right, Z up)'),
    'top': ((0.0, 0.0, 1.0), 'TOP view (looking down -Z; X to the right, Y up)'),
    'right': ((1.0, 0.0, 0.0), 'RIGHT-side view (looking along -X; Y to the right, Z up)'),
    'iso': (None, 'ISOMETRIC view from the front, right and above'),
}
VIEW_ORDER = ('front', 'top', 'right', 'iso')
CREASE_DEG = 30.0          # adjacent faces turning more than this get an inked edge
PX = 720                   # longest side of each picture
SS = 2                     # supersampling
_LIGHT = np.array([-0.35, 0.45, 1.0])
_LIGHT /= np.linalg.norm(_LIGHT)


def _basis(view):
    """(right, up, toward-viewer) unit vectors in world coordinates."""
    d, _ = VIEWS[view]
    if d is None:
        az, el = math.radians(-45.0), math.radians(35.264)
        d = (math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))
    cz = np.array(d, dtype=float)
    cz /= np.linalg.norm(cz)
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(up, cz))) > 0.99:
        up = np.array([0.0, 1.0, 0.0])       # the top view keeps Y up
    cy = up - np.dot(up, cz) * cz
    cy /= np.linalg.norm(cy)
    cx = np.cross(cy, cz)
    return cx, cy, cz


def _grid_step(extent_mm):
    """A grid of 4..12 lines across the larger extent: 1, 2, 5, 10, 20, 50, 100 mm."""
    for g in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500):
        if extent_mm / g <= 12:
            return g
    return 1000


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                      # an older Pillow: the bitmap font
        return ImageFont.load_default()


def render_view(mesh, view, px=PX):
    """One view of a trimesh as (png_bytes, caption, (w_mm, h_mm)).
    `mesh` needs .vertices, .faces, .face_adjacency, .face_adjacency_edges
    and .face_adjacency_angles (trimesh gives all of them)."""
    from PIL import Image, ImageDraw
    cx, cy, cz = _basis(view)
    V = np.asarray(mesh.vertices, dtype=float)
    F = np.asarray(mesh.faces, dtype=np.int64)
    P = V @ np.stack([cx, cy, cz], axis=1)             # camera coords: x right, y up, z toward viewer
    if len(F) == 0 or len(V) == 0:
        raise ValueError('the mesh has no triangles to draw')
    lo = P[:, :2].min(axis=0)
    hi = P[:, :2].max(axis=0)
    ext = hi - lo
    ext[ext < 1e-9] = 1e-9
    title_h = 30
    margin = 22
    scale = min((px - 2 * margin) / float(ext[0]), (px - 2 * margin - title_h) / float(ext[1]))
    W = int(round(ext[0] * scale)) + 2 * margin
    H = int(round(ext[1] * scale)) + 2 * margin + title_h
    W, H = max(W, 240), max(H, 160)
    s = scale * SS
    ox = (W * SS - ext[0] * s) / 2.0
    oy = title_h * SS + (H * SS - title_h * SS - ext[1] * s) / 2.0

    def to_px(xy):
        return (ox + (xy[0] - lo[0]) * s, oy + (hi[1] - xy[1]) * s)

    im = Image.new('RGB', (W * SS, H * SS), 'white')
    dr = ImageDraw.Draw(im)
    # the grid behind the part, on whole multiples of the step
    g = _grid_step(float(ext.max()))
    for k in range(int(math.floor(lo[0] / g)), int(math.ceil(hi[0] / g)) + 1):
        x = to_px((k * g, 0))[0]
        dr.line([(x, oy), (x, oy + ext[1] * s)], fill=(222, 222, 222), width=SS)
    for k in range(int(math.floor(lo[1] / g)), int(math.ceil(hi[1] / g)) + 1):
        y = to_px((0, k * g))[1]
        dr.line([(ox, y), (ox + ext[0] * s, y)], fill=(222, 222, 222), width=SS)

    tri = P[F]                                          # (m, 3, 3)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(n, axis=1)
    ok = norm > 1e-12
    n[ok] /= norm[ok][:, None]
    facing = n[:, 2] > 1e-9
    lam = np.clip(n @ _LIGHT, 0.0, 1.0)
    grey = (95 + 135 * lam).astype(int)
    depth = tri[:, :, 2].mean(axis=1)

    # the fills, and the same triangles by index in a second buffer
    ids = Image.new('I', im.size, -1)
    di = ImageDraw.Draw(ids)
    pts2 = tri[:, :, :2]
    for i in np.argsort(depth):              # far first
        if not facing[i] or not ok[i]:
            continue
        poly = [to_px(p) for p in pts2[i]]
        v = int(grey[i])
        dr.polygon(poly, fill=(v, v, v))
        di.polygon(poly, fill=int(i))
    idbuf = np.asarray(ids, dtype=np.int64)
    Hs, Ws = idbuf.shape

    # crease and silhouette edges, inked where one of their faces is on top
    adj = np.asarray(mesh.face_adjacency)
    if len(adj):
        ang = np.asarray(mesh.face_adjacency_angles)
        edges = np.asarray(mesh.face_adjacency_edges)
        keep = (ang > math.radians(CREASE_DEG)) | (facing[adj[:, 0]] != facing[adj[:, 1]])
        keep &= facing[adj[:, 0]] | facing[adj[:, 1]]
        ink = (28, 28, 28)
        for (a, b), (v0, v1) in zip(adj[keep], edges[keep]):
            x0, y0 = to_px(P[v0, :2])
            x1, y1 = to_px(P[v1, :2])
            n_s = max(2, int(math.hypot(x1 - x0, y1 - y0) / 2.0) + 1)
            t = np.linspace(0.0, 1.0, n_s)
            xs = np.clip(np.rint(x0 + (x1 - x0) * t).astype(int), 0, Ws - 1)
            ys = np.clip(np.rint(y0 + (y1 - y0) * t).astype(int), 0, Hs - 1)
            vis = np.zeros(n_s, dtype=bool)
            for dx in (-1, 0, 1):             # a 3x3 look: the edge sits on a fill boundary
                for dy in (-1, 0, 1):
                    q = idbuf[np.clip(ys + dy, 0, Hs - 1), np.clip(xs + dx, 0, Ws - 1)]
                    vis |= (q == a) | (q == b)
            # draw the visible runs
            start = None
            for k in range(n_s + 1):
                on = k < n_s and vis[k]
                if on and start is None:
                    start = k
                elif not on and start is not None:
                    if k - 1 > start or n_s == 2:
                        dr.line([(x0 + (x1 - x0) * t[start], y0 + (y1 - y0) * t[start]),
                                 (x0 + (x1 - x0) * t[k - 1], y0 + (y1 - y0) * t[k - 1])],
                                fill=ink, width=SS + 1)
                    start = None

    im = im.resize((W, H), Image.LANCZOS)
    dr = ImageDraw.Draw(im)
    label = VIEWS[view][1]
    caption = f'Our build, {label}: {ext[0]:.1f} x {ext[1]:.1f} mm, grid {g:g} mm'
    dr.rectangle([0, 0, W, title_h - 4], fill=(245, 245, 245))
    dr.text((8, 6), f'{view.upper()}  {ext[0]:.1f} x {ext[1]:.1f} mm  (grid {g:g} mm)', fill=(20, 20, 20), font=_font(15))
    out = io.BytesIO()
    im.save(out, 'PNG', optimize=True)
    return out.getvalue(), caption, (float(ext[0]), float(ext[1]))


def _welded(mesh):
    """The mesh with shared vertices merged: a kernel tessellation keeps
    each face's vertices apart, so without this no two faces are adjacent
    and no crease or silhouette would be found."""
    import trimesh
    return trimesh.Trimesh(np.asarray(mesh.vertices), np.asarray(mesh.faces), process=True)


def render_views(mesh, views=VIEW_ORDER, px=PX):
    """[{'view', 'png', 'caption', 'extent_mm'}] for each view, in order."""
    mesh = _welded(mesh)
    out = []
    for v in views:
        png, caption, ext = render_view(mesh, v, px=px)
        out.append({'view': v, 'png': png, 'caption': caption, 'extent_mm': list(ext)})
    return out


def write_renders(renders, out_dir, prefix='render'):
    """Save each picture as <prefix>_<view>.png and a <prefix>s.json index
    ([{'view', 'file', 'caption', 'extent_mm'}]); returns that index."""
    import json
    import os
    index = []
    for r in renders:
        name = f'{prefix}_{r["view"]}.png'
        with open(os.path.join(out_dir, name), 'wb') as f:
            f.write(r['png'])
        index.append({'view': r['view'], 'file': name, 'caption': r['caption'], 'extent_mm': r['extent_mm']})
    with open(os.path.join(out_dir, f'{prefix}s.json'), 'w') as f:
        json.dump(index, f)
    return index


def coarse_mesh(solid):
    """The tessellation the pictures are drawn from: coarser than the
    viewer's, since a picture of 720 px cannot show 0.02 mm anyway and the
    painter's loop is per triangle."""
    from ..pipeline import tessellate_solid
    return tessellate_solid(solid, 0.1, 0.35)
