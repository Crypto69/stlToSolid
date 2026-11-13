"""The texts the server hands to a model: how to work with it, which tool
suits which part, and the workflows as prompts."""

INSTRUCTIONS = """stlToSolid turns meshes (STL, OBJ, 3MF...) and dimensioned drawings into real CAD solids: STEP files and Fusion 360 scripts.

Start with inspect_mesh: it gives the size, the bodies and a suggested method, and warns when the units look wrong. STL carries no units, so check the size with the user before converting something implausible.

convert_mesh, build_recipe and build_cadquery can run for minutes. They wait a little, then answer with a job_id while the job carries on: call job_status with that id until `finished` is true, and tell the user the job is still running. Never start the same conversion twice.

Read the report before calling a result good: `ok` means the run finished, and the report's route and deviations say how faithful the solid is. A faceted route means the part stayed a mesh-like solid.

Files never get overwritten. Say where the results are. Paths are absolute unless the server says otherwise. For guidance on choosing a tool, read stltosolid://guide/which-tool."""

WHICH_TOOL = """# Which stlToSolid tool to use

| The part looks like | Tool | You get |
|---|---|---|
| Machined or CAD-designed: brackets, plates, housings, holes, slots, steps | `convert_mesh` with method `auto` | A STEP with true planes, cylinders, cones, spheres and tori; for simple parts a parametric Fusion script and a CadQuery script |
| Smooth, changing gradually along one direction: caps, handles, shells, bottles | `convert_mesh` with method `loft` | One smooth lofted solid and a Fusion script that repeats the loft |
| A part the user wants to rebuild by hand in Fusion from cross-sections | `plan_xray` then `xray_fusion_script` | A Fusion script with one real sketch per slice, optionally extruded into slabs. No STEP. |
| No mesh, only a dimensioned drawing | the `read-drawing` prompt, then `build_recipe` | A parametric Fusion script (User Parameters, sketches, extrudes) and a STEP |
| A drawing with bends, blends or tilted bosses the recipe cannot draw | `build_cadquery`, when the server allows it | A STEP only |

## Before converting

- Units: STL and OBJ carry none. `units` says what the file's numbers mean and only ever enlarges (`cm` is x10). A cm design exported as mm reads 10x too big and needs `scale` 0.1.
- Bodies: a file can hold many shells. `suggested_bodies` from `inspect_mesh` leaves out the screws and slivers. Pass `bodies` to convert just those, because a 70-body file can take over an hour.
- `trace_section` shows one slice as a picture. Use it to look inside a part, or to find a good plane for a partial loft. Red curves in the picture are open chains: the mesh leaks there.

## Settings and their defaults

| Setting | Default | Meaning |
|---|---|---|
| `tol` | 0.08 mm | Profile fit tolerance |
| `accept_p95` / `accept_max` | 0.25 / 0.26 mm | Surface deviation allowed for a prismatic result |
| `accept_hole_max` | 0.10 mm | Deviation allowed on bores |
| `accept_vol_pct` | 2 % | Volume error allowed |
| `reduce_tol` | 0.05 mm | Faceted output: decimate curved regions within this |
| `slice_mm` | 0.2 mm | Section spacing for loft and X-Ray |
| `slice_join` / `join` | 2.5 mm | Join loose section ends closer than this |
| `slice_trim` / `trim` | 0 mm | Cut slivers thinner than this out of sections |
| `tidy` | off | Sections: re-solve corners as tangent fillets and exact corners; X-Ray scripts add tangent constraints |
| `rails` / `loft` | off | X-Ray scripts: two 90-degree sections as loft rails pinned onto the profiles; with loft, one lofted body (best effort) |
| `sharpen` | 0 mm | Sections: rounds that stay within this of their corner become sharp corners (0.08 is Fusion's Fit Curves feel) |

## Reading the report

`report.mode` is the route: `prismatic` (sketches and extrudes, the cleanest), `facegroup` (one analytic face per fitted region), `faceted` (the fallback, close to the mesh), `loft`, or `mixed` when bodies differ. `report.metrics` holds the measured deviation and volume error against the mesh. `report.output_stats` holds the face count, the surface types and whether the solids are closed. A result with `closed` false imports into CAD as surfaces, not bodies: tell the user.

## Using results in Fusion 360

- A STEP: File, Open, or Insert, Insert into Current Design.
- A `.py` script: Utilities, Add-Ins, Scripts and Add-Ins, the + button, choose the folder holding the script, Run.
- When the client also has Autodesk's Fusion MCP server (`fusion_mcp_execute`), the `send-to-fusion` prompt and the `fusion_handoff` tool run the result directly.
"""


def convert_for_fusion(path):
    return f"""Convert this mesh into a solid the user can model against in Fusion 360: {path}

1. Call inspect_mesh on it. If `unit_warning` is set or the size looks wrong for a real part, ask the user what the size should be and use `units` or `scale`.
2. Choose: use `suggested_method`, and `suggested_bodies` when the file has many bodies. Read stltosolid://guide/which-tool if unsure. Use trace_section when you need to see inside it.
3. Call convert_mesh. If it answers with a job still running, keep calling job_status until `finished` is true. Do not start it again.
4. Read the report. Tell the user the route, the face count and types, and the deviation. If the route is faceted, or `closed` is false, say so plainly and suggest the other method.
5. Call inspect_step on the STEP as a check of what was written.
6. Finish with the file paths and how to open them in Fusion 360."""


def xray_to_fusion(path, axis):
    return f"""Make a Fusion 360 script that draws cross-section sketches of this mesh, for the user to rebuild it by hand: {path}

1. Call inspect_mesh to learn its size and whether the units need fixing.
2. Ask the user nothing you can decide: slice along {axis or "the axis the part is longest on"}, and pick a spacing that gives 10 to 40 slices unless they asked for more.
3. Call trace_section at the middle plane and look at the picture. If it shows red open chains the mesh leaks: raise `join`, or tell the user.
4. Call plan_xray with from and to set to the part's ends along that axis, offsets being measured from the bounding-box centre. Check `count` is under `max`.
5. Call xray_fusion_script. Pass extrude true only if they asked for solid slabs. Report any notes that start with WARNING.
6. Say where the script is. If the client has Autodesk's Fusion MCP server, follow the send-to-fusion prompt. Otherwise give the steps: Utilities, Add-Ins, Scripts and Add-Ins, the + button, choose the folder, Run."""


READ_DRAWING_TAIL = """

# How this server works with that

You are the reader: there is no other model behind this server. Your answer does not go out as bare JSON. Pass the recipe object to the `build_recipe` tool instead of printing it.

1. Read the drawing and write the recipe by the rules above. The exact JSON schema is at stltosolid://blueprint/recipe-schema.
2. Call preview_recipe with the recipe. It answers with the size and four pictures of what you wrote, or the list of checks that failed. Fix what it names.
3. Compare the pictures with the drawing, view by view, once. Change a number only for a label you can quote that disagrees with the picture. If your first reading matches every label, keep it. A second-guess with no label behind it has made parts worse in testing.
4. Call build_recipe. It writes the STEP and the parametric Fusion script. If it answers with a job still running, call job_status until `finished`.
5. Tell the user the size and volume against the drawing, and list every value you marked inferred or low confidence, so they can check those.

If the drawing shows what a recipe cannot draw (bends, blends, tilted bosses, edge fillets), say so. """

READ_DRAWING_CODE_ON = ("Then read stltosolid://blueprint/code-rules and write the part for `build_cadquery` instead, "
                        "which gives a STEP but no parametric Fusion script.")
READ_DRAWING_CODE_OFF = ("This server has the code tool switched off (start it with --allow-code to enable it), so "
                         "build the nearest recipe and list what it leaves out.")


def send_to_fusion(path):
    return f"""Put this stlToSolid result into the user's open Fusion 360 design: {path}

This needs Autodesk's own Fusion MCP server connected to this client as well, with tools named like `fusion_mcp_execute`. Fusion turns that on under Preferences, General, API. If you do not have those tools, say so and give the manual steps instead: Utilities, Add-Ins, Scripts and Add-Ins, the + button, choose the folder, Run.

1. Tell the user this will change the open design, and check a design is open with Fusion's read tool. Fusion can undo it.
2. Call fusion_handoff with the path. It answers with Python to run: the script itself for a `.py`, or a small importer for a `.step`.
3. Run that Python with Fusion's script execution tool, passing the text exactly as given. Read that tool's own schema for how, since Autodesk may change it.
4. Take a screenshot with Fusion's read tool and check the part is there and the right size.
5. Report what you did. If something failed, give Fusion's error text."""
