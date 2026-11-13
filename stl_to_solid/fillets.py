"""Edge rounds as real fillet features.

The face-group engine rebuilds a rounded part exactly, but as faces: a
2 mm round on a box's top edge is a cylindrical face, and the Fusion user
gets no Fillet feature to edit. This module turns such a solid into
"sharp prismatic part + fillets":

  1. `blend_faces`: the rounds — cylinder or torus faces that meet a
     plane or cylinder tangentially along each of their two rails (a
     constant-radius rolling-ball blend), less than half a turn round.
  2. `defeature`: OCC's BRepAlgoAPI_Defeaturing removes them and extends
     the neighbours, leaving the sharp part; the sharp edge each round
     sat on is where the two extended neighbours now meet.
  3. The caller rebuilds the sharp part as sketches + extrudes (the
     prismatic route on its tessellation).
  4. `apply_fillets`: each round goes back on as a fillet of its measured
     radius (snapped to a nominal) on the rebuilt part's edge nearest the
     sharp edge — one operation per radius, in the order a script would
     add them, so the OCC result is the one Fusion and CadQuery will get.
"""
import numpy as np

TANGENT_DEG = 2.0           # neighbours this close to tangent across a rail
MAX_SPAN_DEG = 179.0        # a half-turn or more is a slot end or a bore, not a round
EDGE_MATCH_MM = 0.02        # a rebuilt edge this close to the sharp edge is it


def _faces(shape):
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    out = []
    while ex.More():
        out.append(TopoDS.Face_s(ex.Current()))
        ex.Next()
    return out


def _edges(shape):
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    out = []
    while ex.More():
        out.append(TopoDS.Edge_s(ex.Current()))
        ex.Next()
    return out


def _edge_points(edge, n=5):
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    c = BRepAdaptor_Curve(edge)
    t0, t1 = c.FirstParameter(), c.LastParameter()
    pts = []
    for t in np.linspace(t0, t1, n + 2)[1:-1]:
        p = c.Value(float(t))
        pts.append((p.X(), p.Y(), p.Z()))
    return np.array(pts)


def _normal_at(face, xyz):
    """Outward unit normal of `face` at its point nearest `xyz`."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_REVERSED
    surf = BRep_Tool.Surface_s(face)
    pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(*map(float, xyz)), surf)
    if pr.NbPoints() == 0:
        return None
    u, v = pr.LowerDistanceParameters()
    props = BRepLProp_SLProps(BRepAdaptor_Surface(face), u, v, 1, 1e-7)
    if not props.IsNormalDefined():
        return None
    n = props.Normal()
    n = np.array([n.X(), n.Y(), n.Z()])
    return -n if face.Orientation() == TopAbs_REVERSED else n


def _face_kind(face):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Torus
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    if t == GeomAbs_Cylinder:
        return 'cylinder', s.Cylinder().Radius()
    if t == GeomAbs_Torus:
        return 'torus', s.Torus().MinorRadius()
    return None, None


def _span_deg(face, kind):
    """How far the face turns round its rolling ball (cylinder: u; torus: v)."""
    from OCP.BRepTools import BRepTools
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    return float(np.degrees(u1 - u0 if kind == 'cylinder' else v1 - v0))


def round_candidates(shape):
    """Every face that could be a round (a cylinder or torus less than half
    a turn round), as blend_faces tuples without the tangency test: cheap,
    for prismatic_hint to rule a part out before blend_faces runs."""
    out = []
    for f in _faces(shape):
        kind, r = _face_kind(f)
        if kind is not None and _span_deg(f, kind) < MAX_SPAN_DEG:
            out.append((f, kind, float(r), ()))
    return out


def blend_faces(shape):
    """[(face, kind, radius, (neighbour_a, neighbour_b)), ...]: the faces of
    `shape` that are constant-radius edge rounds."""
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCP.TopoDS import TopoDS
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    cos_t = np.cos(np.radians(TANGENT_DEG))
    out = []
    for f in _faces(shape):
        kind, r = _face_kind(f)
        if kind is None or _span_deg(f, kind) >= MAX_SPAN_DEG:
            continue
        tangent = []
        for e in _edges(f):
            idx = emap.FindIndex(e)
            if idx == 0:
                continue
            nbrs = [TopoDS.Face_s(x) for x in emap.FindFromIndex(idx)]
            others = [g for g in nbrs if not g.IsSame(f)]
            if len(others) != 1:
                continue
            g = others[0]
            p = _edge_points(e, 1)[0]
            nf, ng = _normal_at(f, p), _normal_at(g, p)
            if nf is None or ng is None or float(nf @ ng) < cos_t:
                continue
            if not any(g.IsSame(t) for t in tangent):
                tangent.append(g)
        if len(tangent) == 2 and all(_support_ok(g) for g in tangent):
            out.append((f, kind, float(r), tuple(tangent)))
    return out


def _support_ok(face):
    """A round's neighbour that defeaturing extends cleanly and quickly: a
    plane or a cylinder. Rounds between curved freeform walls (an
    extrusion, a B-spline) or other rounds take OCC minutes to remove and
    seldom leave one sharp edge to fillet again."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
    return BRepAdaptor_Surface(face).GetType() in (GeomAbs_Plane, GeomAbs_Cylinder)


PRISM_DEG = 1.0             # plane normals / cylinder axes this close to the axis or square to it


def prismatic_hint(shape, blends):
    """Cheap test before the (slow) defeaturing: without its rounds, can
    the part be sketches + extrudes? Every other face must be a plane whose
    normal is along or square to one axis, or a cylinder (or extruded
    curve) along that axis — cross bores aside, which the prismatic route
    cuts as features. Returns the axis, or None."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_SurfaceOfExtrusion
    skip = [b[0] for b in blends]
    normals, axes, n_other = [], [], 0
    for f in _faces(shape):
        if any(f.IsSame(b) for b in skip):
            continue
        s = BRepAdaptor_Surface(f)
        t = s.GetType()
        if t == GeomAbs_Plane:
            d = s.Plane().Axis().Direction()
            normals.append((d.X(), d.Y(), d.Z()))
        elif t == GeomAbs_Cylinder:
            d = s.Cylinder().Axis().Direction()
            axes.append((d.X(), d.Y(), d.Z()))
        elif t == GeomAbs_SurfaceOfExtrusion:
            d = s.Direction()
            axes.append((d.X(), d.Y(), d.Z()))
        else:
            n_other += 1
    if n_other:
        return None
    sin_t = np.sin(np.radians(PRISM_DEG))
    for cand in normals + axes:
        a = np.asarray(cand, float)
        ok_planes = all(abs(np.dot(a, n)) < sin_t or abs(abs(np.dot(a, n)) - 1) < sin_t ** 2 / 2
                        for n in normals)
        # a cylinder off the axis is a cross bore (cut as a feature) only
        # when it is square to the axis
        ok_cyls = all(abs(abs(np.dot(a, c)) - 1) < sin_t ** 2 / 2 or abs(np.dot(a, c)) < sin_t
                      for c in axes)
        if ok_planes and ok_cyls:
            return a
    return None


def defeature(shape, blends):
    """The solid with the blend faces removed and their neighbours
    extended, plus, per blend, the sharp edge's sample points (where the
    two extended neighbours meet; None when it cannot be found). Raises
    RuntimeError when OCC cannot remove them."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    from OCP.TopTools import TopTools_ListOfShape
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    df = BRepAlgoAPI_Defeaturing()
    df.SetShape(shape)
    for f, *_ in blends:
        df.AddFaceToRemove(f)
    df.SetRunParallel(False)
    df.SetToFillHistory(True)
    df.Build()
    if not df.IsDone():
        raise RuntimeError('defeaturing failed')
    sharp = df.Shape()
    if not BRepCheck_Analyzer(sharp).IsValid():
        raise RuntimeError('defeatured solid is invalid')

    def image(face):
        mod = df.Modified(face)
        lst = [mod.First()] if mod.Size() else [face]
        return lst[0]
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(sharp, TopAbs_EDGE, TopAbs_FACE, emap)
    edges_of = []
    for f, kind, r, (ga, gb) in blends:
        ia, ib = image(ga), image(gb)
        pts = []
        for i in range(1, emap.Extent() + 1):
            fs = list(emap.FindFromIndex(i))
            if len(fs) == 2 and ((fs[0].IsSame(ia) and fs[1].IsSame(ib))
                                 or (fs[0].IsSame(ib) and fs[1].IsSame(ia))):
                from OCP.TopoDS import TopoDS
                pts.append(_edge_points(TopoDS.Edge_s(emap.FindKey(i))))
        edges_of.append(np.vstack(pts) if pts else None)
    return sharp, edges_of


def _dist_edge_points(edge, pts):
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt
    d = 0.0
    for p in pts:
        v = BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, p))).Vertex()
        ds = BRepExtrema_DistShapeShape(edge, v)
        if not ds.IsDone():
            return np.inf
        d = max(d, ds.Value())
    return d


def match_edges(solid, sharp_pts, tol=EDGE_MATCH_MM):
    """Edges of `solid` (a TopoDS shape) lying on the sharp edge's sample
    points: every sample of the rebuilt edge within `tol` of the sharp
    edge's polyline, and vice versa for the edge's own samples — the
    prismatic rebuild may split one sharp edge into several."""
    from scipy.spatial import cKDTree
    if sharp_pts is None or not len(sharp_pts):
        return []
    # densify the sharp edge's samples so a rebuilt edge's points find a
    # neighbour on it
    tree = cKDTree(sharp_pts)
    spacing = float(np.max(tree.query(sharp_pts, k=2)[0][:, 1])) if len(sharp_pts) > 1 else 0.0
    out = []
    for e in _edges(solid):
        pts = _edge_points(e, 7)
        d, _ = tree.query(pts)
        if np.all(d <= tol + spacing) and _dist_edge_points(e, sharp_pts[:3]) <= tol + spacing:
            out.append(e)
    return out


def apply_fillets(solid, groups, tol=EDGE_MATCH_MM):
    """`solid` (TopoDS) with each group (radius, [sharp-edge sample point
    arrays]) filleted in turn: the edges are matched on the shape as it
    stands before each group (a fillet replaces the shape and its edges).
    Returns (new TopoDS shape, [edge midpoints per group]); raises
    RuntimeError naming the group that will not build."""
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.BRepCheck import BRepCheck_Analyzer
    shape = solid
    mids = []
    for r, sharp_list in groups:
        mk = BRepFilletAPI_MakeFillet(shape)
        edges = []
        for pts in sharp_list:
            for e in match_edges(shape, pts, tol):
                if not any(e.IsSame(x) for x in edges):
                    edges.append(e)
        if not edges:
            raise RuntimeError(f'fillet R{r:g}: no edge found for it')
        for e in edges:
            mk.Add(float(r), e)
        mk.Build()
        if not mk.IsDone():
            raise RuntimeError(f'fillet R{r:g} on {len(edges)} edge(s) does not build')
        mids.append([_edge_points(e, 1)[0].tolist() for e in edges])
        shape = mk.Shape()
        if not BRepCheck_Analyzer(shape).IsValid():
            raise RuntimeError(f'fillet R{r:g} gives an invalid solid')
    return shape, mids
