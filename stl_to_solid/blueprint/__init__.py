"""Blueprint: a dimensioned drawing (front / top / side views with labels)
read into a small feature recipe, then built two ways from the same
recipe: a CadQuery solid (STEP + preview) and a Fusion 360 script that
draws parametric sketches driven by User Parameters and extrudes them.

Recipe = named parameters (mm) + features. A feature is one sketch plane
(XY / XZ / YZ at an offset along its positive normal), one or more
shapes (rect / circle / slot / polygon) and one extrude (new_body / join
/ cut; '+', '-' or symmetric; a distance or through-all). Numbers may be
expressions over the parameters ("body_w/2").

Coordinate convention (the prompt, the validator and both compilers
agree on it): origin at the min corner of the main body's bounding box;
X = width (the front view's horizontal), Y = depth, Z = height. Front
view = XZ, top view = XY, right-side view = YZ. Sketch (u, v) are the two
world axes of the plane in alphabetical order; the plane's normal is the
third axis, positive.

A second language, for parts the recipe cannot draw (a bent plate, a boss
square to a sloped arm, edge fillets): the model writes a CadQuery script
that runs under an allow-list (freeform.py) and gives a STEP and preview,
no Fusion script. read_drawing.py picks the language (or asks the model
to), and after either answer can build the part, draw it from four sides
(render.py) and send the pictures back for correction.
"""
from .expr import ExprError, evaluate, to_fusion
from .schema import SCHEMA, normalize, param_map
from .validate import Report, validate

__all__ = ['ExprError', 'evaluate', 'to_fusion', 'SCHEMA', 'normalize', 'param_map',
           'Report', 'validate']
