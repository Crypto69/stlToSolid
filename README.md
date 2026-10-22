# stlToSolid

Turn an STL or OBJ mesh into a real solid you can model against in Fusion 360.

**[User manual](docs/manual/stlToSolid-User-Manual.md)**: how to use each
tool and what every setting does, with screenshots.
Also as a [PDF](docs/manual/stlToSolid-User-Manual.pdf) and on the website at
[stltosolid.com/docs](https://stltosolid.com/docs/).

## Why

I design 3D-printed parts as a hobby, and I kept needing to model *around*
a mesh: a bracket from Thingiverse, a scanned housing, a controller shell.
Fusion 360's free tier turns a mesh into a solid made of thousands of
little triangles. You can't sketch on it, measure it or build on it.

stlToSolid gives you flat faces, round holes and smooth curves instead,
so the part behaves like something you modelled yourself.

### Before and after

The same `servo_bracket_1.stl`, opened in Fusion 360 three ways. Left:
Fusion's own *Mesh → Solid* (every triangle becomes a face). Middle: the
STL mesh as loaded. Right: the STEP from stlToSolid, with 23 faces, planes
and cylinders, and holes that are real holes.

![Fusion Mesh to Solid (left), the STL mesh (middle) and the stlToSolid STEP (right)](docs/img/side-by-side.png)

| | |
|---|---|
| ![Fusion Mesh to Solid, zoomed](docs/img/fusion-mesh-solid.png) *Fusion Mesh → Solid, zoomed in: one face per triangle, so the "solid" carries all 1,644 facets and cannot be sketched on, filleted or measured like a modelled part.* | ![stlToSolid STEP, zoomed](docs/img/Stl-prism-zoom.png) *stlToSolid, zoomed in: flat faces are single planes, the blend is one cylinder, the edges are where the design has them.* |

### In the app

| | |
|---|---|
| ![Mesh loaded, acceptance gate and options](docs/img/ui-loaded.png) *Mesh loaded, with units and quality limits.* | ![Prismatic result](docs/img/ui-servo-prismatic.png) *Servo bracket: 1,644 triangles → 23 faces (15 planes, 8 cylinders), max deviation 0.060 mm.* |
| ![Face-group result: frame](docs/img/ui-frame-facegroup.png) *Frame with countersinks: 4,200 triangles → 101 faces, max deviation 0.040 mm.* | ![Face-group result: joystick claw](docs/img/ui-claw-facegroup.png) *Joystick claw: 1,902 triangles → 250 faces, including 43 cylinders and 20 spheres.* |

## The four tools

- **Mesh → Solid.** Drop in a mesh and get a STEP file with real planes,
  cylinders, cones and fillets. Simple parts also come with a Fusion script
  that rebuilds them as editable sketches and extrudes.
- **Sliced Loft.** For smooth, rounded shapes like caps, handles and shells.
  It slices the mesh into outlines and lofts them into one smooth solid.
- **X-Ray.** Traces a stack of outlines between two planes and gives you a
  Fusion script with one sketch per slice. It can also extrude the slices
  into a stepped solid.
- **Blueprint.** Starts from a picture, not a mesh. Drop in a dimensioned
  drawing, and an AI model of your choice reads the numbers and builds a
  parametric Fusion script and a STEP. You can fix any number it misread.

Results open in Fusion 360 (or any CAD program that reads STEP).

There is also a **Fusion 360 add-in, Mesh to Outline** (coming soon), that
does the X-Ray tracing inside Fusion itself. The
[user manual](docs/manual/stlToSolid-User-Manual.md) has a step-by-step guide
for every tool, the add-in included.

## Get it

| | |
|---|---|
| **Mac** (Apple Silicon) | Download the app from [Releases](https://github.com/Crypto69/stlToSolid/releases/latest). No Python or Docker needed. |
| **Windows** (64-bit) | Download from [Releases](https://github.com/Crypto69/stlToSolid/releases/latest). Unzip and open. |
| **Your NAS or a server** | One Docker container you open from any browser: `docker compose up --build`, then go to `http://localhost:8321`. Step-by-step NAS guide: [docs/DEPLOY-NAS.md](docs/DEPLOY-NAS.md). |
| **Command line** | `pip install .` then `stltosolid part.stl` makes `part.step`. |

The Mac and Windows apps are the same app as the web version, in their own
window.

### Running a Fusion script

Some results come with a `.py` script for Fusion 360. To run one:

1. Put the `.py` file in an empty folder.
2. In Fusion: **Utilities → Add-Ins → Scripts and Add-Ins → +**, and pick
   that folder.
3. Press **Run**.

## More detail

**Using the app:** the [user manual](docs/manual/stlToSolid-User-Manual.md)
([PDF](docs/manual/stlToSolid-User-Manual.pdf)) walks through every tool and
every setting.

**How it works inside:** how each tool works, every setting, the API, measured results and the
version history are in [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Licence

**PolyForm Noncommercial 1.0.0**. See [LICENSE](LICENSE). Free for personal,
hobby, educational and research use. Commercial use needs the author's
permission, so get in touch.

Third-party parts keep their own licences (CadQuery Apache-2.0, OpenCascade
LGPL-2.1 with exception, trimesh MIT, shapely/networkx BSD). The optional
`scan` extra installs pymeshlab, which is GPL-3.0. It is not part of this
package's licence and is only installed if you ask for it.
