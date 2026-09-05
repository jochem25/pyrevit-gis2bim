# -*- coding: utf-8 -*-
"""Dekvloer-contour terugleggen op HSB-wandvlak.

Zoekt dekvloeren (43_FL_dekvloer*) die enkele mm in HSB-wanden
(21_hsb_245/12,5) steken en verschuift de parallelle contourlijn van de
vloersketch naar het binnenvlak van de wand. Werkt op groepsleden via
SketchEditScope (geen ungroup). Modi: dry-run (rapport, alles wordt
teruggerold), alleen selectie, alle dekvloeren.

IronPython 2.7 / Revit 2025 API.
"""

__title__ = "Dekvloer\nHSB"
__author__ = "KBA"
__doc__ = "Dekvloercontour 2,5 mm terug op HSB-wandvlak (ook in groups)"

import os
import datetime
import traceback

from Autodesk.Revit.DB import (
    FilteredElementCollector, Wall, Floor, Options, ViewDetailLevel, Solid,
    PlanarFace, BooleanOperationsUtils, BooleanOperationsType,
    BoundingBoxIntersectsFilter, Outline, XYZ, Line, ModelCurve,
    SketchEditScope, Transaction, TransactionGroup, ElementTransformUtils,
    ElementId, WorksharingUtils, CheckoutStatus, Element,
)


def tname(el):
    """Typenaam van wand/vloer (IronPython-veilig)."""
    t = doc.GetElement(el.GetTypeId())
    return Element.Name.__get__(t) if t is not None else ""
from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
out = script.get_output()
logger = script.get_logger()

MM = 304.8
WALL_TYPE_KEY = "21_hsb_245"
FLOOR_TYPE_PREFIX = "43_FL_dekvloer"
MIN_DEPTH_MM = 0.5     # kleiner = negeren (ruis)
MAX_DEPTH_MM = 5.0     # groter = ander probleem, niet automatisch fixen
EXTENT_TOL_MM = 50.0   # lijn mag zoveel buiten de wandlengte steken

# ---------------------------------------------------------------- helpers
opt = Options()
opt.DetailLevel = ViewDetailLevel.Fine


def solids(e):
    res = []
    geo = e.get_Geometry(opt)
    if geo is None:
        return res
    for g in geo:
        if isinstance(g, Solid) and g.Volume > 0:
            res.append(g)
    return res


def wall_frame(w):
    c = w.Location.Curve
    p0, p1 = c.GetEndPoint(0), c.GetEndPoint(1)
    d = (p1 - p0).Normalize()
    n = XYZ(-d.Y, d.X, 0)
    return p0, p1, d, n


def wall_faces_along_n(w, n):
    vals = set()
    for s in solids(w):
        for f in s.Faces:
            if isinstance(f, PlanarFace) and abs(abs(f.FaceNormal.DotProduct(n)) - 1) < 1e-6:
                vals.add(f.Origin.DotProduct(n))
    return sorted(vals)


def clash_depth(w, f, n):
    """Diepte (mm) van de doorsnijding wand/vloer langs wandnormaal n."""
    pts = []
    for a in solids(w):
        for b in solids(f):
            try:
                iv = BooleanOperationsUtils.ExecuteBooleanOperation(
                    a, b, BooleanOperationsType.Intersect)
            except Exception:
                continue
            if iv is None or iv.Volume < 1e-9:
                continue
            for ed in iv.Edges:
                cv = ed.AsCurve()
                pts.append(cv.GetEndPoint(0))
                pts.append(cv.GetEndPoint(1))
    if not pts:
        return 0.0
    pr = [p.DotProduct(n) for p in pts]
    return (max(pr) - min(pr)) * MM


def group_label(e):
    if e.GroupId and e.GroupId.IntegerValue > 0:
        g = doc.GetElement(e.GroupId)
        return g.Name
    return "-"


def editable(e):
    if not doc.IsWorkshared:
        return True, ""
    st = WorksharingUtils.GetCheckoutStatus(doc, e.Id)
    if st == CheckoutStatus.OwnedByOtherUser:
        return False, "owned by other user"
    return True, ""


# ------------------------------------------------------------- analysis
def find_pairs(floors, walls):
    """Lijst van (floor, wall, depth_mm, n, sign, target_offset)."""
    pairs = []
    for f in floors:
        fbb = f.get_BoundingBox(None)
        if fbb is None:
            continue
        for w in walls:
            wbb = w.get_BoundingBox(None)
            if wbb is None:
                continue
            if (wbb.Max.X < fbb.Min.X or wbb.Min.X > fbb.Max.X or
                    wbb.Max.Y < fbb.Min.Y or wbb.Min.Y > fbb.Max.Y or
                    wbb.Max.Z < fbb.Min.Z or wbb.Min.Z > fbb.Max.Z):
                continue
            p0, p1, d, n = wall_frame(w)
            depth = clash_depth(w, f, n)
            if depth < MIN_DEPTH_MM:
                continue
            pairs.append((f, w, depth, p0, p1, d, n))
    return pairs


def plan_moves(f, w, depth, p0, p1, d, n):
    """Bepaal welke sketchlijnen van vloer f verschoven moeten worden.

    Retourneert lijst van dicts {curve_index, a, b, move_vec, note}."""
    faces = wall_faces_along_n(w, n)
    if len(faces) < 2:
        return [], "geen wandvlakken"
    lo, hi = faces[0], faces[-1]
    sk = doc.GetElement(f.SketchId)
    moves = []
    notes = []
    wl0 = p0.DotProduct(d)
    wl1 = p1.DotProduct(d)
    wlo, whi = min(wl0, wl1), max(wl0, wl1)
    for ca in sk.Profile:
        for c in ca:
            if not isinstance(c, Line):
                continue
            a, b = c.GetEndPoint(0), c.GetEndPoint(1)
            cd = (b - a).Normalize()
            if abs(abs(cd.DotProduct(d)) - 1) > 1e-4:
                continue
            pos = a.DotProduct(n)
            # lijn moet binnen de wand liggen
            if pos < lo - 1e-6 or pos > hi + 1e-6:
                continue
            # dichtstbijzijnde vlak = binnenvlak; verschuif erheen
            to_lo = pos - lo
            to_hi = hi - pos
            if to_lo <= to_hi:
                delta = -to_lo
            else:
                delta = to_hi
            dmm = abs(delta) * MM
            if dmm < MIN_DEPTH_MM:
                continue
            if dmm > MAX_DEPTH_MM:
                notes.append("lijn %.1f mm in wand: te diep, overslaan" % dmm)
                continue
            # lengte-check: lijn binnen wandlengte (+tol)?
            la, lb = a.DotProduct(d), b.DotProduct(d)
            llo, lhi = min(la, lb), max(la, lb)
            over = max(wlo - llo, lhi - whi, 0) * MM
            if over > EXTENT_TOL_MM:
                notes.append("lijn steekt %.0f mm buiten wand: overslaan (handmatig)" % over)
                continue
            moves.append({"a": a, "b": b, "vec": n.Multiply(delta), "dmm": dmm})
    return moves, "; ".join(notes)


def find_model_curve(sk, a, b):
    """Zoek de ModelCurve in de sketch die bij eindpunten a,b hoort."""
    for eid in sk.GetAllElements():
        mc = doc.GetElement(eid)
        if not isinstance(mc, ModelCurve):
            continue
        c = mc.GeometryCurve
        if not isinstance(c, Line):
            continue
        ca, cb = c.GetEndPoint(0), c.GetEndPoint(1)
        if (ca.IsAlmostEqualTo(a) and cb.IsAlmostEqualTo(b)) or \
                (ca.IsAlmostEqualTo(b) and cb.IsAlmostEqualTo(a)):
            return mc
    return None


def apply_moves(f, moves):
    sk = doc.GetElement(f.SketchId)
    scope = SketchEditScope(doc, "Dekvloer op HSB-vlak")
    scope.Start(sk.Id)
    try:
        t = Transaction(doc, "verschuif contourlijn")
        t.Start()
        n_moved = 0
        for m in moves:
            mc = find_model_curve(sk, m["a"], m["b"])
            if mc is None:
                raise Exception("sketchlijn niet gevonden")
            ElementTransformUtils.MoveElement(doc, mc.Id, m["vec"])
            n_moved += 1
        t.Commit()
        scope.Commit(FailurePreproc())
        return n_moved
    except Exception:
        try:
            scope.Cancel()
        except Exception:
            pass
        raise


from Autodesk.Revit.DB import IFailuresPreprocessor, FailureProcessingResult


class FailurePreproc(IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        for fm in fa.GetFailureMessages():
            if fm.GetSeverity().ToString() == "Warning":
                fa.DeleteWarning(fm)
            else:
                return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


# ---------------------------------------------------------------- main
def main():
    mode = forms.CommandSwitchWindow.show(
        ["Dry-run (alleen rapport)", "Alleen geselecteerde vloeren (in Edit Group)",
         "Alle losse dekvloeren (groups overslaan)"],
        message="Dekvloer-contour terug op HSB-wandvlak")
    if not mode:
        return
    dry = mode.startswith("Dry")
    # gegroepeerde vloeren alleen aanpakken als de gebruiker ze expliciet
    # selecteert (binnen Edit Group); anders overslaan + checklist
    skip_grouped = not mode.startswith("Alleen")

    walls = [w for w in FilteredElementCollector(doc).OfClass(Wall)
             if WALL_TYPE_KEY in tname(w)]
    if mode.startswith("Alleen"):
        floors = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
        floors = [f for f in floors if isinstance(f, Floor)]
        if not floors:
            forms.alert("Selecteer eerst een of meer vloeren.", exitscript=True)
    else:
        floors = [f for f in FilteredElementCollector(doc).OfClass(Floor)
                  if tname(f).startswith(FLOOR_TYPE_PREFIX)]

    out.print_md("**HSB-wanden:** %d  **Vloeren:** %d  **Modus:** %s" % (len(walls), len(floors), mode))

    tg = TransactionGroup(doc, "Dekvloer op HSB-vlak")
    tg.Start()
    rows = []
    grouped_types = {}
    n_ok = n_skip = n_err = 0
    try:
        pairs = find_pairs(floors, walls)
        out.print_md("Gevonden wand/vloer-doorsnijdingen: **%d**" % len(pairs))
        # groepeer per vloer
        by_floor = {}
        for p in pairs:
            by_floor.setdefault(p[0].Id.IntegerValue, []).append(p)
        for fid, plist in by_floor.items():
            f = doc.GetElement(ElementId(fid))
            if skip_grouped and f.GroupId and f.GroupId.IntegerValue > 0:
                gt = doc.GetElement(f.GroupId).GroupType
                gtn = Element.Name.__get__(gt)
                grouped_types.setdefault(gtn, []).append(fid)
                rows.append((fid, tname(f), gtn, "GROUP", "via Edit Group + 'Geselecteerde vloeren'"))
                n_skip += 1
                continue
            ok, why = editable(f)
            if not ok:
                rows.append((fid, tname(f), group_label(f), "SKIP", why))
                n_skip += 1
                continue
            # herbereken: door group-propagatie kan clash al weg zijn
            moves = []
            notes = []
            for (_f, w, depth, p0, p1, d, n) in plist:
                depth_now = clash_depth(w, f, n)
                if depth_now < MIN_DEPTH_MM:
                    continue
                if depth_now > MAX_DEPTH_MM:
                    notes.append("wand %d: %.1f mm, te diep" % (w.Id.IntegerValue, depth_now))
                    continue
                mv, note = plan_moves(f, w, depth_now, p0, p1, d, n)
                if note:
                    notes.append("wand %d: %s" % (w.Id.IntegerValue, note))
                moves.extend(mv)
            if not moves:
                rows.append((fid, tname(f), group_label(f), "SKIP",
                             "; ".join(notes) or "geen verschuifbare lijn (al opgelost?)"))
                n_skip += 1
                continue
            desc = ", ".join("%.2f mm" % m["dmm"] for m in moves)
            try:
                nm = apply_moves(f, moves)
                # verificatie
                rest = max([clash_depth(w, f, n) for (_f, w, _dp, _p0, _p1, _d, n) in plist] or [0])
                rows.append((fid, tname(f), group_label(f), "OK",
                             "%d lijn(en) verschoven: %s; rest %.2f mm%s" % (
                                 nm, desc, rest, ("; " + "; ".join(notes)) if notes else "")))
                n_ok += 1
            except Exception as ex:
                rows.append((fid, tname(f), group_label(f), "ERROR", str(ex)[:200]))
                n_err += 1
                logger.debug(traceback.format_exc())
    finally:
        if dry:
            tg.RollBack()
        else:
            tg.Assimilate()

    # rapport
    out.print_md("### Resultaat: OK %d, SKIP %d, ERROR %d %s" % (
        n_ok, n_skip, n_err, "(dry-run: alles teruggerold)" if dry else ""))
    out.print_table(
        table_data=[[out.linkify(ElementId(r[0])), r[1], r[2], r[3], r[4]] for r in rows],
        columns=["Vloer", "Type", "Group", "Status", "Detail"])
    if grouped_types:
        out.print_md("### Checklist groeptypen (Edit Group -> vloer selecteren -> knop 'Geselecteerde vloeren' -> Finish): %d" % len(grouped_types))
        out.print_table(
            table_data=[[gtn, len(ids), " ".join(out.linkify(ElementId(i)) for i in ids[:6])]
                        for gtn, ids in sorted(grouped_types.items())],
            columns=["Groeptype", "Vloeren", "Voorbeeld-vloeren"])
    logdir = os.path.join(os.path.dirname(__file__), "log")
    if not os.path.isdir(logdir):
        os.makedirs(logdir)
    fn = os.path.join(logdir, "dekvloer_hsb_%s.csv" % datetime.datetime.now().strftime("%y%m%d_%H%M%S"))
    with open(fn, "w") as fh:
        fh.write("floor_id;type;group;status;detail\n")
        for r in rows:
            fh.write("%s;%s;%s;%s;%s\n" % r)
    out.print_md("Log: `%s`" % fn)


main()
