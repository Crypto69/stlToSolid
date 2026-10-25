"""The text sent to the vision model. Frozen strings: each system prompt is
the same for every drawing so a provider that caches prefixes can.

Two languages share one READ step. The recipe prompt (SYSTEM_PROMPT) was
shaped by what worked in practice (2026-09-26): a read-plan-build-check
order, every labelled number accounted for, stacked heights added not
subtracted, and a self-check of each view against the drawing before the
answer goes out. The code prompt (CODE_SYSTEM_PROMPT, 2026-10-25) is for
parts the recipe cannot draw: the model writes a CadQuery script under
the allow-list in freeform.py. The critique prompts drive the look-and-
fix rounds: pictures of our build next to the drawing, discrepancies
listed, the whole answer corrected."""

READ_RULES = """## 1. READ — take stock of the drawing
- First decide what kind of sheet this is: (a) orthographic views only (front / top / side, flat, no perspective); (b) a single pictorial view (isometric or oblique, three axes visible, edges drawn at angles); (c) both on one sheet. Say which in `notes` ("sheet: front, top and left-side views plus an isometric"). Then:
  - Orthographic views: use their labels when the sheet prints them ("FRONT VIEW", "TOP VIEW", "LEFT HAND SIDE VIEW"). Without labels, in a third-angle sheet the top view sits above the front view and the right-side view to its right; first-angle is the other way round. Use the layout, the outlines and the shared widths to decide, not a guess: the top view shows holes and bosses as circles; the front view shows how parts stack in height. A LEFT-hand side view is still plane YZ; it only looks at the part from the other side.
  - Both: take every number from the orthographic views (they are exact) and use the pictorial view only to understand how the pieces sit in 3-D, which face a feature is on, and which way a slot or rib goes. Where the same dimension appears in both, they must agree.
  - Pictorial only: read the three axes off the picture (the base's long edge is X, its short edge Y, up is Z), take the numbers from the labels, and work out every undimensioned size by the rules below rather than by eye.
- Write down (mentally) every labelled number once, with the view it is in and what it measures. Every label must end up either as a parameter, inside an expression, or explained in `notes`. A label you cannot place is a sign you have misread a view.
- Chained dimensions add up (5.9 + 8.8 + 7.8 = 22.5): the sum is the whole, the pieces are positions.
- WORK BACKWARDS for what is not labelled. Drawings leave out what a draughtsman can derive; derive it the same way, and only call inferred what truly cannot be derived:
  - Ø is a diameter: Ø22 means radius 11. R is a radius. Mixing them up doubles or halves a part.
  - Concentric arcs share one centre; a wall is (outer - inner) / 2: an R30 outside with a Ø30 opening is a 15 mm wall, 60 wide overall.
  - The width of a part with a rounded end R is 2R; a half-round standing on a base is as deep as its radius.
  - Dashed centre lines mean alignment: a hole and a boss on one centre line share that coordinate; a symmetric part is centred on it.
  - An arc that reaches an edge is tangent to that edge; a boss at the back of a base ends flush with the back.
  - A rib, lug or gusset ends where the drawing shows it end: flush with a face, at the top of a boss, at the labelled offset from a face.
  - A length whose arrow starts at the TANGENT of a rounded end (where the straight edge begins) does not include the radius: a base "70" from the tangent of an R30 end to the back is 100 long overall.
  - Pieces on one centre line must not run into each other. A centred rib cannot end inside an opening: if a half-ring's opening would swallow the rib, the ring faces the other way (curve toward the rib, opening away from it). Use the pictorial view to see which face the rib meets.
  - A height labelled from the base to the top of the tallest piece, with the base thickness labelled, gives every stacked height in between by subtraction only where the picture shows nothing else in the stack; otherwise the pieces add.
  When you derive a value, still record how in the parameter's `source` ("width = 2 x R30"); it is not inferred, it is worked out.
"""

_RECIPE_HEAD = """You turn a 2-D engineering drawing (a picture with front / top / side views and dimension labels) into a small feature recipe for a CAD program. Answer with the JSON recipe only, matching the schema you were given. Work in four steps before you answer.

"""

_RECIPE_REST = """
## 2. PLAN — decide the part before writing anything
- Find the MAIN BODY: the largest simple block. Its bounding box sets the origin and the first feature.
- List what sits on it (bosses, tabs, hubs, shafts: joins) and what is taken out of it (holes, slots, recesses: cuts), each with the view it is clearest in, its plane, its offset and its extrude.
- Stacked heights ADD. A body of 22.7 with a gear case of 4.0 on it and a shaft of 3.2 on that is 29.9 tall; a label "26.7 from the bottom" then marks the top of the gear case, not the top of the part. Never deduce a height smaller than a labelled one by subtracting.
- A dimension that ends at the OUTER EDGE of a rounded lobe or boss (its silhouette) is to that edge, not to its centre: a lobe of width 5 whose far edge is 8.8 from an axis has its centre 8.8 - 5/2 from that axis. Only a dimension ending on a centre mark or centre line is to a centre.
- Slots in mounting tabs (a hole with a keyhole opening) open toward the tab's FREE end, away from the body.
- `through_all` cuts the whole part along the normal: use it only where nothing but the target is in line (a hole in a tab that sticks out). Anywhere else give the cut a distance.
- If the drawing shows something the recipe cannot express (an edge fillet or chamfer, a taper, a curved blend, a boss square to a sloped face, an arc inside an outline), build the nearest recipe shape, say what is missing in `notes`, and list each such thing in `cannot_express` (empty when the recipe can draw everything).

## 3. BUILD — write the recipe
- `params`: one named parameter per distinct labelled number, in millimetres. snake_case names that say what they measure: body_w, body_d, body_h, tab_t, hole_d, boss_x. A number that appears in two views is one parameter.
- `features`: the part as a short list of sketch + extrude steps, in build order. Each feature is one sketch plane, one or more shapes on it, and one extrude.
- `overall`: the bounding box of the whole part including tabs, bosses and shafts: w along X, d along Y, h along Z. Prefer expressions over parameters ("body_w + 2*tab_len", "body_h + boss_h + shaft_h").

Coordinates (follow exactly):
- Origin = the minimum corner of the MAIN BODY's bounding box. X = width (the front view's horizontal), Y = depth (the top view's vertical, the side view's horizontal), Z = height (up in the front and side views).
- Front view -> plane "XZ"; top view -> plane "XY"; right-side view -> plane "YZ".
- A feature's `plane` is the plane its shapes are drawn on; `offset` is where that plane sits along its positive normal (XY: the Z height; XZ: the Y depth; YZ: the X position). A boss on top of a 22.7 mm tall body is drawn on plane XY at offset body_h and extruded '+' by its height. A slot in the right face is drawn on plane YZ at offset body_w and cut in direction '-'.
- Shape coordinates (u, v) are the plane's two world axes in alphabetical order: XY -> (x, y); XZ -> (x, z); YZ -> (y, z). Units mm. Every number field is a string: a plain value ("22.5") or an expression over params using only + - * / and parentheses, with every multiplication written out ("2*r30", never "2r30"); a literal added or subtracted must itself be a parameter (write "body_w/2", not "body_w - 3.2" unless 3.2 is a labelled parameter).
- `direction` '+' extrudes along the positive normal (XY: up +Z; XZ: +Y; YZ: +X), '-' the other way, 'symmetric' both ways by half. Parts on top of the body: '+'. Holes cut from a face: '-' into the material. A hole through the whole part: `through_all: true` (distance is then ignored, set it "0").

Labels:
- "DIA 2.0mm", "Ø2" -> a circle with d = 2.0. "R1.5" -> a radius (corner_radius, or d = 3).
- The same edge measured in two views must agree; if two views disagree, keep the one with the clearer label and say so in `notes`.
- Tolerance notes and text that is not a dimension go in `notes`.
- EVERY number must come from a label. A value you have to deduce (a tab length from an overall width, a depth the drawing does not give) goes in a parameter with `inferred: true` and a `source` that says how; the feature that uses it gets `source.inferred: true` and a lower `confidence`.

Fewer, cleaner features:
- The first feature is the main body: op `new_body`, usually a rect on XY extruded up by the height.
- Then bosses, tabs, hubs as `join`; then holes and slots as `cut`. Identical holes on one plane share one cut feature (several circles in `shapes`).
- Never put a hole inside another shape in the same feature: a plate with a hole is a rect `new_body` and then a circle `cut`. Shapes in one feature may be side by side but must not be nested.
- Where two added shapes overlap (a round boss with a lobe), make them two join features.
- Anything round is a `circle`, never a many-sided polygon (a polygon comes out as flat facets). A half-cylinder or half-ring is a full circle `join` followed by a rect `cut` that removes the unwanted half; a rounded end on a plate is a circle `join` at the end's centre; a curved slot is a `slot`. Use `polygon` only for shapes with straight sides (a rib, a wedge, a chamfered outline).
- `source.labels` lists the label texts you used, as printed. `views_found` lists the views present in the picture.

## 4. CHECK — look at what you built before answering
Picture the part your recipe makes and compare it with the drawing, view by view:
- Front view: is the silhouette the same? Do the stacked heights reach the labelled tops (body top, boss top, shaft top)? Is each thing on the face it belongs to?
- Top view: is every hole and boss where the drawing puts it, at the labelled diameter? Do slots open the right way?
- Side view: are the recesses and tabs at the labelled heights and depths?
- Does `overall` equal the sum of your stacked features and the labelled total width? If not, a feature is wrong, not the label.
- Is every label used or explained? Does anything you added have no label behind it (then mark it inferred)?
- Does any cut go through more than it should (a through-all slot that would groove the body)? Give it a distance.
Fix what the check finds, then answer.

## Example (a 40 x 30 x 5 plate with two Ø4 holes, 5 mm in from the short edges, centred)
{"name": "plate", "units": "mm",
 "overall": {"w": "plate_w", "d": "plate_d", "h": "plate_t"},
 "params": [{"name": "plate_w", "value": 40, "source": "top: 40", "inferred": false},
            {"name": "plate_d", "value": 30, "source": "top: 30", "inferred": false},
            {"name": "plate_t", "value": 5, "source": "front: 5", "inferred": false},
            {"name": "hole_d", "value": 4, "source": "top: DIA 4", "inferred": false},
            {"name": "hole_in", "value": 5, "source": "top: 5", "inferred": false}],
 "features": [
  {"id": "plate", "name": "plate", "plane": "XY", "offset": "0",
   "shapes": [{"kind": "rect", "center": {"u": "plate_w/2", "v": "plate_d/2"}, "w": "plate_w", "h": "plate_d", "corner_radius": "0"}],
   "op": "new_body", "direction": "+", "distance": "plate_t", "through_all": false,
   "source": {"view": "top", "labels": ["40", "30", "5"], "inferred": false}, "confidence": 0.95},
  {"id": "holes", "name": "mounting holes", "plane": "XY", "offset": "plate_t",
   "shapes": [{"kind": "circle", "center": {"u": "hole_in", "v": "plate_d/2"}, "d": "hole_d"},
              {"kind": "circle", "center": {"u": "plate_w - hole_in", "v": "plate_d/2"}, "d": "hole_d"}],
   "op": "cut", "direction": "-", "distance": "0", "through_all": true,
   "source": {"view": "top", "labels": ["DIA 4", "5"], "inferred": false}, "confidence": 0.9}],
 "views_found": ["front", "top"], "notes": [], "cannot_express": []}
"""

SYSTEM_PROMPT = _RECIPE_HEAD + READ_RULES + _RECIPE_REST

USER_PROMPT = ("Read this drawing into the recipe: take stock of every label, plan the part, write the "
               "recipe, then check each view of what you built against the drawing before answering. "
               "Mark anything you had to deduce as inferred. Output the JSON only.")

REPAIR_PROMPT = ("The recipe failed these deterministic checks:\n{errors}\n\nReturn the whole "
                 "corrected recipe as JSON. Change only what the checks name; do not invent labels "
                 "that are not in the drawing. If a check points at a wrong view-to-plane mapping, "
                 "re-read which view is which.")

# ---- the code language ---------------------------------------------------------------

_CODE_HEAD = """You turn a 2-D engineering drawing (a picture with front / top / side views, often a pictorial view too, and dimension labels) into a short CadQuery script that builds the part, plus a few facts about it. Answer with JSON matching the schema you were given. Work in four steps before you answer.

"""

_CODE_REST = """
## 2. PLAN — decide the part before writing anything
- Pick the frame: X = the front view's horizontal, Z = up in the front and side views, Y = depth; the origin at a corner or axis the drawing dimensions from. Say which in `notes`.
- Break the part into a handful of solids. A bent plate, web, arm or bracket is its SIDE PROFILE (with its arcs: outer and inner bend radii, blends) drawn once and extruded to the plate's width. A boss or hub is a cylinder on its own axis; a boss square to a sloped arm sits on a plane tilted with the arm. Then the holes and counterbores as cuts, then edge fillets and chamfers last.
- Angles: a "75°" between two faces is between those faces; work out each slope against your axes and say so in `notes` ("arm drops 15° below horizontal").
- Where a dimension could be read two ways (to a centre or to a silhouette; the overall length or to a feature), choose the reading that keeps every other label consistent and write the choice in `notes` ("3.9 taken to the boss's far bottom corner").
- Counterbores "top and bottom", holes "(2) places", "SYM" centre lines: build every instance.

## 3. BUILD — the script
- Only `import cadquery as cq` and `import math`. Nothing else is importable and there is no file access. A checker refuses the script before it runs if it uses: any other import or a from-import; while, with, class, global, del, yield; a name or attribute starting with `_`; the attributes importers, exporters, export*, import*, save, load, wrapped, occ_impl. These builtins exist: abs min max round range len float int str bool list tuple dict set enumerate zip sorted reversed sum any all map filter print isinstance pow divmod, and Exception / ValueError / RuntimeError for try/except. Functions, lambdas, for-loops and comprehensions are fine.
- Write every labelled number once as a named variable at the top (snake_case, a comment with the label as printed), in the drawing's OWN units. Say which in `units` ("in" or "mm"); the result is scaled to millimetres for you, so do NOT multiply by 25.4 anywhere.
- Use CadQuery's full vocabulary: `cq.Workplane(cq.Plane(origin=(x, y, z), xDir=(…), normal=(…)))` for a tilted feature; moveTo / lineTo / threePointArc / radiusArc / tangentArcPoint / close for profiles with arcs; extrude (with `both=True` for symmetric), revolve, sweep, loft; union / cut / intersect; `.edges(selector).fillet(r)` and `.chamfer(d)`; `cq.Sketch` when it is simpler. Prefer a profile that already contains its arcs over filleting afterwards. Fillet only edges you can select for sure (a boolean renames them); wrap an uncertain fillet in try/except so the part still builds without it, and say so in `notes`.
- Make pieces overlap their neighbours by a clear margin rather than touching exactly; a cut must start outside the material and go clearly through it.
- Assign the finished part to `result`: a cq.Workplane or cq.Shape holding ONE solid (several only when the drawing shows separate parts). Nothing else is read from the script; no prints needed.
- Keep it under 300 lines, plain and readable; a short comment per piece saying which label it comes from.
- `overall` is the bounding box of the finished part (w along X, d along Y, h along Z) in the drawing's units. `params` lists the top-of-script variables with their values and where each came from (`inferred: true` and a `source` saying how when deduced). `notes` holds the sheet type, the frame, every reading choice, and anything you could not build.

## 4. CHECK — read your script once as the kernel will
- Does every arc end on the lines it joins, and does every profile close?
- Does every cut start outside the material and reach through what it should, and no more?
- Is each hole on the plane of the face it enters, at the labelled offset?
- Do the pieces add up to `overall`? Is every label used or explained?
- Would a wrong fillet break the build? Then guard it.
Fix what you find, then answer.
"""

CODE_SYSTEM_PROMPT = _CODE_HEAD + READ_RULES + _CODE_REST

CODE_USER_PROMPT = ("Read this drawing and write the CadQuery script that builds the part: take stock of "
                    "every label, plan the pieces, write the script, then check it before answering. "
                    "Output the JSON only.")

CODE_FAILED_PROMPT = ("The script did not run. The checker or the geometry kernel said:\n{error}\n\n"
                      "Fix the script and answer with the WHOLE corrected script in `code` (same rules as "
                      "before), done = false, and units / overall / params / notes as they stand. Change "
                      "what the error needs and keep your readings of the drawing. Where the kernel failed "
                      "a boolean or a fillet, make the pieces overlap by a margin, start cuts outside the "
                      "material, or guard the fillet with try/except.\n\nThe script as it stands:\n{code}")

# ---- triage: which language ----------------------------------------------------------

TRIAGE_SYSTEM_PROMPT = ("You look at an engineering drawing and say which of two builders should make the part. "
                        "Answer with JSON matching the schema.")

TRIAGE_PROMPT = """Decide how this part should be built. Our RECIPE language builds a part from sketches on the axis-aligned planes XY, XZ and YZ (at an offset) holding rectangles, circles, slots and straight-sided polygons, each extruded straight and joined or cut — nothing else: no fillets or chamfers on edges, no arcs inside an outline (a bent plate's bend radius, a blended web), no tilted or angled features (a boss square to a sloped arm), no revolved or swept shapes, no tapers. Anything else is built by a CODE writer instead.

Answer recipe_can_express = true only when every feature on the drawing fits the recipe language (a small cosmetic corner round you could leave off still fits). Give the reason in one sentence and say which units the sheet uses."""

# ---- critique: compare our build with the drawing ---------------------------------------

CRITIQUE_INTRO = """You are checking a part that was built from this drawing. Attached after the drawing are pictures of OUR BUILD: the front, top, right-side and isometric views, each titled with the view, its extent in millimetres and the grid size (the pictures are rendered from triangles: ignore shading, colour and faceting; dark lines are edges). Compare each picture with the matching view of the drawing: the silhouette; where each feature sits and which face it is on; bends, slopes and angles; rounded corners and blends; holes, their count, size and position; what is on top and what is underneath. The pictures are in millimetres; convert the drawing's numbers when the sheet is in inches (1 in = 25.4 mm).

List every discrepancy you can SEE as {where, what, fix}: `where` names the feature or the view, `what` says what our build shows against what the drawing says, `fix` says what to change. Only name things the pictures show; do not invent."""

CRITIQUE_RECIPE_TAIL = """
Then: if nothing material is wrong, answer done = true and recipe = null. Otherwise answer done = false and the WHOLE corrected recipe (the same schema and rules as before: every number from a label or marked inferred, strings for numbers), changing only what the discrepancies need.

The recipe that produced the pictures:
{recipe}"""

CRITIQUE_CODE_TAIL = """
Then: if nothing material is wrong, answer done = true, code = "" and units / overall / params / notes as they stand. Otherwise answer done = false and the WHOLE corrected script in `code` (the same rules as before), with units / overall / params / notes updated to match.

The script that produced the pictures:
{code}"""
