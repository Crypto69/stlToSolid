"""Short questions the worker answers in its own process: a look at a mesh, a
traced section, an X-Ray plan or script, a look at a STEP. The MCP server asks
through the worker rather than in its own process, because a giant mesh once
took 15.5 GB in the body split and a bad STEP can crash the geometry kernel:
either costs this process, never the server.

Each call takes (input path, output path, params) and returns a JSON-able dict
for result.json."""
import base64

from .analysis import sanitize

# A share of the surface in flat regions above this reads as a designed,
# machined part; below it as a smooth, flowing one.
PRISMATIC_FLAT_SHARE = 0.5
# Bodies listed in full in a mesh's overview; a 72-shell controller is mostly
# screws, and the model only needs the first few to choose.
MAX_LISTED_BODIES = 40


def _flat_share(m):
    """The share of the surface lying in flat regions of more than one
    triangle. Coplanar neighbours are what a CAD export is made of and a
    smooth or scanned surface is not."""
    area = float(m.area)
    if area <= 0:
        return 0.0
    return min(1.0, float(m.facets_area.sum()) / area)


def inspect_mesh(in_path, out_path, params):
    from stl_to_solid.mesh_prep import UNIT_SCALE, load_mesh
    from .analysis import body_list, mesh_stats
    k = UNIT_SCALE.get(params.get('units', 'mm'), 1.0) * float(params.get('scale', 1.0))
    stats = mesh_stats(in_path)
    listed = body_list(in_path)
    bodies = listed['bodies']
    share = _flat_share(load_mesh(in_path))
    out = []
    for b in bodies[:MAX_LISTED_BODIES]:
        out.append({'index': b['index'], 'triangles': b['triangles'], 'watertight': b['watertight'],
                    'volume_mm3': None if b['volume'] is None else round(b['volume'] * k ** 3, 3),
                    'size_mm': [round(v * k, 3) for v in b['size']], 'suggested': b['suggested']})
    method = 'auto' if share >= PRISMATIC_FLAT_SHARE else 'loft'
    return sanitize({
        'triangles': stats['triangles'], 'vertices': stats['vertices'],
        'watertight': stats['watertight'], 'file_size': stats['file_size'],
        'bbox_mm': [round(v * k, 3) for v in stats['bbox_mm']],
        'surface_area_mm2': round(stats['surface_area_mm2'] * k ** 2, 3),
        'volume_mm3': None if stats['volume_mm3'] is None else round(stats['volume_mm3'] * k ** 3, 3),
        'units_suggestion': stats['units_suggestion'], 'unit_warning': stats['unit_warning'],
        'flat_surface_share': round(share, 3), 'suggested_method': method,
        'suggested_method_why': (
            f'{share * 100:.0f}% of the surface lies in flat regions, which reads as a designed part: '
            'try method auto first.' if method == 'auto' else
            f'Only {share * 100:.0f}% of the surface lies in flat regions, which reads as a smooth '
            'shape: try method loft, and fall back to auto if the part has real flats.'),
        'body_count': len(bodies), 'bodies': out, 'bodies_listed': len(out),
        'bodies_truncated': bool(listed.get('truncated')) or len(bodies) > len(out),
        'suggested_bodies': [b['index'] for b in bodies if b['suggested']],
    })


def loft_axis(in_path, out_path, params):
    from .sections import loft_axis as pick
    return sanitize(pick(in_path, params.get('units', 'mm'), params.get('scale', 1.0),
                         params.get('slice_mm', 0.2), params.get('join', 2.5), params.get('trim', 0.0)))


def trace_section(in_path, out_path, params):
    from .section_image import section_png
    from .sections import trace
    sec = trace(in_path, params['axis'], params.get('offset', 0.0), params.get('tol', 0.08),
                params.get('units', 'mm'), params.get('scale', 1.0), params.get('join', 2.5),
                params.get('outline', False), params.get('trim', 0.0),
                tidy=params.get('tidy', False), sharpen=params.get('sharpen', 0.0))
    png = section_png(sec.pop('polylines'), params['axis'], sec['at'])
    sec['png_b64'] = base64.b64encode(png).decode('ascii')
    return sanitize(sec)


def plan_xray(in_path, out_path, params):
    from .xray import plan
    return sanitize(plan(in_path, params['axis'], params['from'], params['to'], params['step'],
                         params.get('units', 'mm'), params.get('scale', 1.0)))


def xray_script(in_path, out_path, params):
    from .xray import over_cap_message, plan, script
    p = plan(in_path, params['axis'], params['from'], params['to'], params['step'],
             params.get('units', 'mm'), params.get('scale', 1.0))
    if p['over_cap']:
        raise ValueError(over_cap_message(p['count']))
    s = script(in_path, params['axis'], params['from'], params['to'], params['step'], p['offsets'],
               tol=params.get('tol', 0.08), units=params.get('units', 'mm'), scale=params.get('scale', 1.0),
               join=params.get('join', 2.5), outline=params.get('outline', False),
               trim=params.get('trim', 0.0), extrude=params.get('extrude', False),
               budget_s=params.get('budget_s'), tidy=params.get('tidy', False),
               sharpen=params.get('sharpen', 0.0), rails=params.get('rails', False),
               loft=params.get('rails', False) and params.get('loft', False))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(s.pop('text'))
    return sanitize(s)


def inspect_step(in_path, out_path, params):
    import cadquery as cq
    from .analysis import step_stats
    from stl_to_solid.pipeline import accurate_volume
    out = step_stats(in_path)
    wp = cq.importers.importStep(in_path)
    vals = [v for v in wp.vals() if isinstance(v, cq.Shape)]
    if vals:
        shape = vals[0] if len(vals) == 1 else cq.Compound.makeCompound(vals)
        bb = shape.BoundingBox()
        out['bbox_mm'] = [round(bb.xlen, 4), round(bb.ylen, 4), round(bb.zlen, 4)]
        out['valid'] = bool(shape.isValid())
        solids = shape.Solids()
        try:
            out['volume_mm3'] = round(float(sum(accurate_volume(s) for s in solids)), 3)
        except Exception:
            out['volume_mm3'] = None
    else:
        out.update(bbox_mm=None, valid=False, volume_mm3=None)
    return sanitize(out)


CALLS = {'inspect_mesh': inspect_mesh, 'loft_axis': loft_axis, 'trace_section': trace_section,
         'plan_xray': plan_xray, 'xray_script': xray_script, 'inspect_step': inspect_step}
