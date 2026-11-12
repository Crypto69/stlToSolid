"""X-Ray: where a stack of section planes goes and the Fusion 360 script that
draws them. Shared by the web route and the MCP tool, so both make the same
script with the same notes."""
import numpy as np

from .sections import XRAY_MAX, half_extent, trace_stack


def plan(path, axis, frm, to, step, units='mm', scale=1.0):
    """The offsets (mm from the bounding-box centre) of a stack from `frm`
    to `to` every `step`, the end plane always included and both ends kept
    just inside the part. Cheap: no cut, no fit."""
    from stl_to_solid.section_fit import stack_offsets
    h = half_extent(path, axis, units, scale)
    offs = stack_offsets(frm, to, step, h)
    return {'axis': axis, 'from': frm, 'to': to, 'step': step, 'half_extent': h,
            'offsets': offs, 'count': len(offs), 'max': XRAY_MAX, 'over_cap': len(offs) > XRAY_MAX}


def rails_preview(path, axis, frm, to, units='mm', scale=1.0):
    """The raw rail cuts of a stack from `frm` to `to` (mm from the
    centre, clamped like the stack's own ends), as viewer polylines:
    {'axis', 'polylines', 'kind': 'rail'}."""
    from stl_to_solid.section_fit import stack_offsets
    from .sections import _mesh, rails_preview as cut
    h = half_extent(path, axis, units, scale)
    offs = stack_offsets(frm, to, max(abs(to - frm), 1e-6), h)     # just the two clamped ends
    m = _mesh(path, units, scale)
    c = float(m.bounding_box.centroid['xyz'.index(axis)])
    lo, hi = offs[0], offs[-1]
    return {'axis': axis, 'kind': 'rail', 'polylines': cut(path, axis, c + lo, c + hi, units, scale)}


def over_cap_message(count):
    return (f'{count} slices is over the limit of {XRAY_MAX}; raise the '
            'spacing or narrow the range')


def script(path, axis, frm, to, step, offsets, tol=0.08, units='mm', scale=1.0, join=2.5,
           outline=False, trim=0.0, extrude=False, budget_s=None, tidy=False, sharpen=0.0,
           rails=False, loft=False):
    """Trace every plane in `offsets` and write the Fusion script: a
    construction plane and a fully enclosed sketch per slice. `extrude` also
    extrudes every slice to the next plane, so the stack comes out as solid
    slabs: the last slice one spacing further but never past the part's far
    face, and an end plane closer than half a spacing to the slice before it
    is drawn only, that slice's slab running through instead (no hair-thin
    slab). `tidy` / `sharpen` trace with tidy corners and draw the loops
    through shared sketch points with tangent constraints. `rails` adds
    two 90-degree sections along the stack as rails (stl_to_solid.rails:
    their crossings pinned onto the fitted profiles, every number at 6
    decimals so they still touch), and `loft` a best-effort loft of one new
    body through the profiles along the complete rails (`extrude` is then
    ignored). Returns {'text', 'name', 'slices', 'notes', 'rails'}; 'name'
    is the file name's tail after the part's stem."""
    from stl_to_solid.fusion_export import emit_fusion_xray_script
    from stl_to_solid.section_fit import STACK_EDGE_MM, open_outline_note, stack_extrudes
    rail_list = []
    if rails:
        from stl_to_solid.section_fit import lift_section
        from .sections import rails_for
        secs2d = trace_stack(path, axis, offsets, tol, units, scale, join, outline, trim, budget_s,
                             tidy=tidy, sharpen=sharpen, two_d=True)
        # the rails first: they add their crossings to the splines' fit points
        rail_list = rails_for(path, axis, secs2d, tol, units, scale)
        secs = []
        for s2 in secs2d:
            meta = {k: s2[k] for k in ('axis', 'at', 'offset', 'centre', 'extent', 'bbox_centre', 'empty')
                    if k in s2}
            sec = lift_section(s2) if not s2.get('empty') else {
                'origin': np.asarray(s2['origin']).tolist(), 'normal': np.asarray(s2['normal']).tolist(),
                'loops': [], 'open': [], 'polylines': [], 'areas_mm2': [], 'stats': s2['stats']}
            sec.update(meta)
            secs.append(sec)
        extrude = False if loft else extrude
    else:
        loft = False
        secs = trace_stack(path, axis, offsets, tol, units, scale, join, outline, trim, budget_s,
                           tidy=tidy, sharpen=sharpen)
    n = len(secs)
    exts = [None] * n
    if extrude:
        # the offsets ascend, so every slab goes towards +axis
        far = secs[-1]['centre'] + secs[-1]['extent'] / 2.0 - STACK_EDGE_MM
        exts = stack_extrudes([s['at'] for s in secs], step, far)
    sections = []
    for i, sec in enumerate(secs):
        sections.append({'origin': sec['origin'], 'normal': sec['normal'],
                         'name': f"xray {axis.upper()}={sec['at']:.2f} mm ({i + 1}/{n})",
                         'loops': sec['loops'], 'open': sec['open'],
                         'areas_mm2': sec.get('areas_mm2', []), 'extrude_mm': exts[i]})
    notes = []
    n_trim = sum(1 for s in secs if s['stats'].get('trimmed'))
    if n_trim:
        k = sum(s['stats']['trimmed'] for s in secs)
        notes.append(f"{k} sliver{'s' if k != 1 else ''} trimmed in {n_trim} of {n} slices "
                     f"(Trim slivers up to {trim:g} mm); if the part has real walls that thin, "
                     "lower it")
    n_br = [s for s in secs if s['stats'].get('bridged')]
    if n_br:
        k = sum(s['stats']['bridged'] for s in n_br)
        big = max(s['stats'].get('bridge_max', 0.0) for s in n_br)
        notes.append(f"outline closed across {k} mesh gap{'s' if k != 1 else ''} in {len(n_br)} of "
                     f"{n} slices (largest {big:.2f} mm): holes in the mesh skin, filled with a "
                     "straight wall")
    open_note = open_outline_note([(f"{axis.upper()}={s['at']:.2f} mm", s['stats']) for s in secs], join)
    if open_note:
        notes.insert(0, 'WARNING: ' + open_note)
    lo, hi = sorted((frm, to))
    title = f'X-Ray {axis.upper()} {lo:+.1f}..{hi:+.1f} mm every {step:g} mm'
    if sharpen > 0:
        k = sum(s['stats'].get('sharpened', 0) for s in secs)
        if k:
            notes.append(f"{k} round{'s' if k != 1 else ''} under {sharpen:g} mm drawn as sharp "
                         "corners (Sharpen rounds below)")
    for i, r in enumerate(rail_list):
        r['name'] = f'rail {i + 1}/{len(rail_list)}'
    if rails:
        k = sum(1 for r in rail_list if r['complete'])
        notes.append(f"{len(rail_list)} rail{'s' if len(rail_list) != 1 else ''} traced, {k} touching "
                     "every profile" + ('' if k else ': no rail reaches every section, so a loft runs '
                                         'without rails'))
    text = emit_fusion_xray_script(sections, title=title, notes=notes, tidy=tidy,
                                   rails=rail_list if rails else None, loft=loft)
    suffix = '_loft' if loft and n > 1 else ('_solid' if extrude and n > 1 else '')
    return {'text': text, 'name': f'_xray_{axis}{lo:+.1f}_{hi:+.1f}_s{step:g}{suffix}.py',
            'slices': n, 'notes': notes,
            'rails': {'count': len(rail_list), 'complete': sum(1 for r in rail_list if r['complete'])}}
