"""The pictures of a built part: four views, titled, with visible edges,
and an index file the panel and the read loop both use."""
import io
import json
import os

import pytest

pytest.importorskip('PIL')

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'sg90_recipe.json')


@pytest.fixture(scope='module')
def sg90_solid():
    from stl_to_solid.blueprint.validate import validate
    from stl_to_solid.blueprint.build_cq import build
    with open(FIX) as f:
        rec = json.load(f)
    return build(validate(rec).resolved, measure=False).solid


def _png(data):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def test_four_views_with_titles_and_edges(sg90_solid):
    import numpy as np
    from stl_to_solid.blueprint.render import VIEW_ORDER, coarse_mesh, render_views
    m = coarse_mesh(sg90_solid)
    out = render_views(m)
    assert [r['view'] for r in out] == list(VIEW_ORDER)
    front = next(r for r in out if r['view'] == 'front')
    top = next(r for r in out if r['view'] == 'top')
    # the extents are the part's own: 32.2 wide, 29.9 tall in front, 11.8 deep in top
    assert front['extent_mm'] == pytest.approx([32.2, 29.9], abs=0.05)
    assert top['extent_mm'] == pytest.approx([32.2, 11.8], abs=0.05)
    assert 'FRONT' in front['caption'] and '32.2 x 29.9 mm' in front['caption'] and 'grid' in front['caption']
    for r in out:
        im = _png(r['png'])
        assert im.mode == 'RGB' and 600 <= max(im.size) <= 720, r['view']
        px = np.asarray(im)
        # the part is drawn (greys), edges are inked (dark pixels), paper is white
        assert (px < 60).all(axis=2).sum() > 200, r['view']
        assert ((px > 80) & (px < 240)).all(axis=2).sum() > 5000, r['view']
    # two views of one part are different pictures
    assert front['png'] != top['png']


def test_write_renders_index(sg90_solid, tmp_path):
    from stl_to_solid.blueprint.render import coarse_mesh, render_views, write_renders
    idx = write_renders(render_views(coarse_mesh(sg90_solid), views=('front', 'iso')), str(tmp_path))
    assert [i['view'] for i in idx] == ['front', 'iso']
    assert sorted(os.listdir(tmp_path)) == ['render_front.png', 'render_iso.png', 'renders.json']
    with open(tmp_path / 'renders.json') as f:
        assert json.load(f) == idx
    assert idx[0]['file'] == 'render_front.png' and 'caption' in idx[0]


def test_box_from_a_plain_trimesh():
    import trimesh
    from stl_to_solid.blueprint.render import render_view
    png, caption, ext = render_view(trimesh.creation.box((40, 20, 10)), 'top')
    assert ext == pytest.approx((40, 20))
    assert 'TOP' in caption and '40.0 x 20.0 mm' in caption
    assert _png(png).size[0] == 720


def test_empty_mesh_is_refused():
    import trimesh
    from stl_to_solid.blueprint.render import render_view
    with pytest.raises(ValueError):
        render_view(trimesh.Trimesh(), 'front')
