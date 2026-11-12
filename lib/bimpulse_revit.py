# -*- coding: utf-8 -*-
"""Revit side of the BIMPulse Issues dashboard: finds an issue's elements in
the open model (or its links) and selects, zooms, isolates and colours them.

Every function here must run in a Revit API context (the dashboard calls them
through an ExternalEvent).
"""
import System
from System.Collections.Generic import List

from Autodesk.Revit.DB import (BoundingBoxXYZ, BuiltInParameter, Color, Element, ElementCategoryFilter, ElementId,
                               FilteredElementCollector, Level, OverrideGraphicSettings, Reference,
                               RevitLinkInstance, TemporaryViewMode, Transaction, View3D, ViewFamily,
                               ViewFamilyType, XYZ, FillPatternElement)

import bimpulse_bcf

VIEW_NAME = "BIMPulse Issues 3D"
PAD = 3.0  # feet around the section box (~1 m)
SCAN_LIMIT = 400  # most elements matched by family/type or a parameter value
SEVERITY_RGB = {"critical": (239, 68, 68), "error": (249, 115, 22), "warning": (245, 158, 11), "info": (0, 160, 227)}


def make_id(n):
    try:
        return ElementId(System.Int64(int(n)))  # Revit 2024+
    except Exception:
        return ElementId(int(n))


def name_of(el):
    """Element.Name (IronPython can't always read it straight off a type)."""
    try:
        return el.Name or ""
    except Exception:
        try:
            return Element.Name.__get__(el) or ""
        except Exception:
            return ""


def id_value(eid):
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


class Hit(object):
    """An element found for an issue: in the host model, or in a link."""

    def __init__(self, element, link=None):
        self.element = element
        self.link = link  # RevitLinkInstance, or None for the host model

    @property
    def where(self):
        return "Link: {}".format(self.link.Name.split(" : ")[0]) if self.link else "This model"

    def box(self):
        bb = self.element.get_BoundingBox(None)
        if bb is None:
            return None
        if not self.link:
            return bb.Min, bb.Max
        tf = self.link.GetTotalTransform()
        pts = [tf.OfPoint(XYZ(x, y, z)) for x in (bb.Min.X, bb.Max.X) for y in (bb.Min.Y, bb.Max.Y)
               for z in (bb.Min.Z, bb.Max.Z)]
        return (XYZ(min(p.X for p in pts), min(p.Y for p in pts), min(p.Z for p in pts)),
                XYZ(max(p.X for p in pts), max(p.Y for p in pts), max(p.Z for p in pts)))


class Resolution(object):
    def __init__(self):
        self.hits = []
        # One per issue element: (status, first Hit or None, hit count); status is
        # found, link, many (matched by type or value), mismatch (same id, other category) or missing.
        self.rows = []
        self.model_wide = False

    @property
    def host_ids(self):
        return [h.element.Id for h in self.hits if not h.link]

    @property
    def found(self):
        return sum(1 for row in self.rows if row[0] in ("found", "link", "many"))


class Finder(object):
    """Finds issue elements in one model and its loaded links, with caches."""

    def __init__(self, doc):
        self.doc = doc
        self.key = doc.PathName or doc.Title
        self._links = None
        self._ifc = {}
        self._types = {}
        self._cats = None
        self.cache = {}

    # -- lookups --

    def links(self):
        if self._links is None:
            self._links = []
            for inst in FilteredElementCollector(self.doc).OfClass(RevitLinkInstance):
                try:
                    ldoc = inst.GetLinkDocument()
                except Exception:
                    ldoc = None
                if ldoc is not None:
                    self._links.append((inst, ldoc))
        return self._links

    def _ifc_index(self, doc):
        key = doc.PathName or doc.Title
        if key not in self._ifc:
            index = {}
            for el in FilteredElementCollector(doc).WhereElementIsNotElementType():
                try:
                    g = bimpulse_bcf.ifc_guid_from_unique_id(el.UniqueId)
                    if g:
                        index[g] = el
                    p = el.get_Parameter(BuiltInParameter.IFC_GUID)
                    if p and p.HasValue:
                        index[p.AsString()] = el
                except Exception:
                    continue
            self._ifc[key] = index
        return self._ifc[key]

    def _category(self, name):
        if not name:
            return None
        if self._cats is None:
            self._cats = {}
            for c in self.doc.Settings.Categories:
                self._cats[c.Name.lower()] = c.Id
        return self._cats.get(name.lower())

    def _collector(self, category):
        col = FilteredElementCollector(self.doc).WhereElementIsNotElementType()
        cat = self._category(category)
        if cat is not None:
            col = col.WherePasses(ElementCategoryFilter(cat))
        return col

    def _by_type(self, family, type_name, category):
        key = (family or "", type_name or "", category or "")
        if key not in self._types:
            found = []
            for el in self._collector(category):
                try:
                    t = self.doc.GetElement(el.GetTypeId())
                except Exception:
                    t = None
                if t is None:
                    continue
                fam = getattr(t, "FamilyName", "") or ""
                tname = name_of(t)
                if (not family or fam == family) and (not type_name or tname == type_name):
                    found.append(el)
                    if len(found) >= SCAN_LIMIT:
                        break
            self._types[key] = found
        return self._types[key]

    def _by_value(self, parameter, value, category):
        found = []
        for el in self._collector(category):
            p = el.LookupParameter(parameter)
            if p is None or not p.HasValue:
                continue
            v = p.AsString() if p.AsString() is not None else p.AsValueString()
            if (v or "").strip() == value.strip():
                found.append(el)
                if len(found) >= SCAN_LIMIT:
                    break
        return found

    def _level(self, name):
        for lv in FilteredElementCollector(self.doc).OfClass(Level):
            if name_of(lv) == name:
                return lv
        return None

    @staticmethod
    def _category_ok(el, expected):
        if not expected or el.Category is None:
            return True
        return el.Category.Name.lower() == expected.lower()

    def _find_one(self, e):
        """(status, [Hit]) for one element entry from the BCF."""
        uid, eid, ifc, cat = e.get("unique_id"), e.get("element_id"), e.get("ifc_guid"), e.get("category")
        if uid:  # a UniqueId is unique across models, so trust it over an id
            el = self.doc.GetElement(uid)
            if el is not None:
                return "found", [Hit(el)]
            for inst, ldoc in self.links():
                el = ldoc.GetElement(uid)
                if el is not None:
                    return "link", [Hit(el, inst)]
        if eid:
            el = self.doc.GetElement(make_id(eid))
            if el is not None and self._category_ok(el, cat):
                return "found", [Hit(el)]
            for inst, ldoc in self.links():
                lel = ldoc.GetElement(make_id(eid))
                if lel is not None and self._category_ok(lel, cat):
                    return "link", [Hit(lel, inst)]
            if el is not None:  # same id, different category: likely another model's element
                return "mismatch", [Hit(el)]
        if ifc and not uid and not eid:
            el = self._ifc_index(self.doc).get(ifc)
            if el is not None:
                return "found", [Hit(el)]
            for inst, ldoc in self.links():
                el = self._ifc_index(ldoc).get(ifc)
                if el is not None:
                    return "link", [Hit(el, inst)]
        if uid or eid or ifc:
            return "missing", []
        if cat == "Levels" and e.get("level"):
            lv = self._level(e["level"])
            return ("found", [Hit(lv)]) if lv else ("missing", [])
        if e.get("family") or e.get("type"):
            els = self._by_type(e.get("family"), e.get("type"), cat)
            return ("many" if len(els) > 1 else "found", [Hit(x) for x in els]) if els else ("missing", [])
        if e.get("parameter") and e.get("value"):
            els = self._by_value(e["parameter"], e["value"], cat)
            return ("many" if len(els) > 1 else "found", [Hit(x) for x in els]) if els else ("missing", [])
        return "missing", []

    def resolve(self, topic):
        if topic.key in self.cache:
            return self.cache[topic.key]
        r = Resolution()
        r.model_wide = not topic.elements
        seen = set()
        for e in topic.elements:
            try:
                status, hits = self._find_one(e)
            except Exception:
                status, hits = "missing", []
            r.rows.append((status, hits[0] if hits else None, len(hits)))
            for h in hits:
                k = (id_value(h.element.Id), h.link.Id.ToString() if h.link else "")
                if k not in seen:
                    seen.add(k)
                    r.hits.append(h)
        self.cache[topic.key] = r
        return r


# --- actions ---

def _ids(ids):
    out = List[ElementId]()
    for i in ids:
        out.Add(i)
    return out


def select(uidoc, hits):
    host = [h.element.Id for h in hits if not h.link]
    linked = [h for h in hits if h.link]
    if linked:
        try:  # Revit 2023+: links can be selected by reference
            refs = List[Reference]()
            for h in hits:
                ref = Reference(h.element)
                refs.Add(ref.CreateLinkReference(h.link) if h.link else ref)
            uidoc.Selection.SetReferences(refs)
            return
        except Exception:
            pass
    uidoc.Selection.SetElementIds(_ids(host))


def union_box(hits):
    boxes = [b for b in (h.box() for h in hits) if b]
    if not boxes:
        return None
    lo = XYZ(min(b[0].X for b in boxes), min(b[0].Y for b in boxes), min(b[0].Z for b in boxes))
    hi = XYZ(max(b[1].X for b in boxes), max(b[1].Y for b in boxes), max(b[1].Z for b in boxes))
    return lo, hi


def _ui_view(uidoc, view):
    for uv in uidoc.GetOpenUIViews():
        if uv.ViewId == view.Id:
            return uv
    return None


def zoom(uidoc, hits):
    """Zooms the active view (Revit picks a view when it can't show them)."""
    host = [h.element.Id for h in hits if not h.link]
    if host and len(host) == len(hits):
        uidoc.ShowElements(_ids(host))
        return
    box = union_box(hits)
    uv = _ui_view(uidoc, uidoc.ActiveView)
    if box and uv:
        pad = XYZ(PAD, PAD, PAD)
        uv.ZoomAndCenterRectangle(box[0] - pad, box[1] + pad)
    elif host:
        uidoc.ShowElements(_ids(host))


def issues_view(doc):
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if not v.IsTemplate and name_of(v) == VIEW_NAME:
            return v
    vft = None
    for t in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if t.ViewFamily == ViewFamily.ThreeDimensional:
            vft = t
            break
    tx = Transaction(doc, "BIMPulse: issues 3D view")
    tx.Start()
    try:
        v = View3D.CreateIsometric(doc, vft.Id)
        v.Name = VIEW_NAME
        tx.Commit()
    except Exception:
        tx.RollBack()
        raise
    return v


def show_3d(uidoc, hits):
    doc = uidoc.Document
    box = union_box(hits)
    if box is None:
        raise ValueError("These elements have no geometry to show")
    view = issues_view(doc)
    pad = XYZ(PAD, PAD, PAD)
    tx = Transaction(doc, "BIMPulse: section box")
    tx.Start()
    try:
        if view.ViewTemplateId != ElementId.InvalidElementId:
            view.ViewTemplateId = ElementId.InvalidElementId
        sb = BoundingBoxXYZ()
        sb.Min, sb.Max = box[0] - pad, box[1] + pad
        view.IsSectionBoxActive = True
        view.SetSectionBox(sb)
        tx.Commit()
    except Exception:
        tx.RollBack()
        raise
    if uidoc.ActiveView.Id != view.Id:
        uidoc.ActiveView = view
    select(uidoc, hits)
    uv = _ui_view(uidoc, view)
    if uv:
        uv.ZoomAndCenterRectangle(box[0] - pad, box[1] + pad)
    return view


def isolate(uidoc, hits):
    view = uidoc.ActiveView
    host = [h.element.Id for h in hits if not h.link]
    links = list({h.link.Id.ToString(): h.link.Id for h in hits if h.link}.values())
    tx = Transaction(uidoc.Document, "BIMPulse: isolate issue")
    tx.Start()
    try:
        view.IsolateElementsTemporary(_ids(host + links))
        tx.Commit()
    except Exception:
        tx.RollBack()
        raise


def _solid_fill(doc):
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            if fp.GetFillPattern().IsSolidFill:
                return fp.Id
        except Exception:
            continue
    return None


def colour(uidoc, by_severity):
    """Colours host elements in the active view. by_severity: {severity: [ElementId]}.
    Returns the ids coloured, so they can be cleared later."""
    doc, view = uidoc.Document, uidoc.ActiveView
    solid = _solid_fill(doc)
    done = []
    tx = Transaction(doc, "BIMPulse: colour issues")
    tx.Start()
    try:
        for sev in ("info", "warning", "error", "critical"):  # worst last, so it wins
            r, g, b = SEVERITY_RGB[sev]
            c = Color(r, g, b)
            ogs = OverrideGraphicSettings()
            ogs.SetProjectionLineColor(c)
            if solid is not None:
                try:
                    ogs.SetSurfaceForegroundPatternId(solid)
                    ogs.SetSurfaceForegroundPatternColor(c)
                except AttributeError:  # Revit 2018 and older
                    ogs.SetProjectionFillPatternId(solid)
                    ogs.SetProjectionFillColor(c)
            for eid in by_severity.get(sev, []):
                try:
                    view.SetElementOverrides(eid, ogs)
                    done.append(eid)
                except Exception:
                    continue
        tx.Commit()
    except Exception:
        tx.RollBack()
        raise
    return done


def clear(uidoc, ids):
    doc, view = uidoc.Document, uidoc.ActiveView
    tx = Transaction(doc, "BIMPulse: clear issue colours")
    tx.Start()
    try:
        blank = OverrideGraphicSettings()
        for eid in ids:
            try:
                if doc.GetElement(eid) is not None:
                    view.SetElementOverrides(eid, blank)
            except Exception:
                continue
        try:
            view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
        except Exception:
            pass
        tx.Commit()
    except Exception:
        tx.RollBack()
        raise


def describe(el):
    """A short "Family : Type" or name for an element found in the model."""
    try:
        t = el.Document.GetElement(el.GetTypeId())
        fam = getattr(t, "FamilyName", "") if t else ""
        if t is not None:
            return "{} : {}".format(fam, name_of(t)) if fam else name_of(t)
    except Exception:
        pass
    return name_of(el)


# ---------- fixing issues in the model ----------

# Failed fields that are names, not parameters (Object Naming).
NAME_FIELDS = ("Type name", "Family name")


def parameter_names(field):
    """Parameter names to try for a failed field: the audit's column name, then the usual Revit
    names for it (NBS Uniclass shared parameters, the COBie extension's)."""
    names = [field]
    parts = field.split()
    if len(parts) >= 2 and parts[0] == "Uniclass":  # "Uniclass Pr" / "Uniclass Pr Description"
        table = parts[1]
        if len(parts) == 2:
            names += ["Classification.Uniclass.{}.Number".format(table), "Classification.Uniclass.{}.Code".format(table),
                      "Uniclass {} Number".format(table), "Uniclass {} Code".format(table),
                      "Uniclass_{}_Number".format(table), "Uniclass_{}_Code".format(table)]
        else:
            names += ["Classification.Uniclass.{}.Description".format(table), "Uniclass {} Title".format(table),
                      "Uniclass_{}_Description".format(table), "Classification.Uniclass.{}.Title".format(table)]
    elif not field.startswith("COBie."):
        names += ["COBie.Type." + field, "COBie.Component." + field, "COBie.Space." + field, "COBie." + field]
    return names


def _squash(name):
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _owners(el):
    owners = [el]
    try:
        t = el.Document.GetElement(el.GetTypeId())
        if t is not None:
            owners.append(t)
    except Exception:
        pass
    return owners


def _find_parameter(el, field):
    """(parameter, owner) on the element or its type, or (None, None). Exact names first, then the
    same names ignoring case, spaces, dots and underscores."""
    owners = _owners(el)
    names = parameter_names(field)
    for name in names:
        for owner in owners:
            p = owner.LookupParameter(name)
            if p is not None:
                return p, owner
    wanted = set(_squash(n) for n in names)
    for owner in owners:
        for p in owner.Parameters:
            if _squash(p.Definition.Name) in wanted:
                return p, owner
    return None, None


def similar_parameters(el, field):
    """Names of the element's (and its type's) parameters that look related to the field, for messages."""
    words = [w for w in ("uniclass", "classification", "cobie") if w in field.lower()] or [_squash(field)[:6]]
    found = []
    for owner in _owners(el):
        for p in owner.Parameters:
            name = p.Definition.Name
            if any(w in name.lower() for w in words) and name not in found:
                found.append(name)
    return found


def _set_value(p, value):
    kind = str(p.StorageType)
    if kind == "String":
        return p.Set(value)
    if kind == "Integer":
        return p.Set(int(value))
    if kind == "Double":
        return p.SetValueString(value)
    raise ValueError("can't be set from text")


def apply_fix(doc, hits, field, value):
    """Sets a failed field on the issue's elements in the host model: a parameter (on the
    element or its type), or for Object Naming the type or family name. One undoable
    transaction. Returns (changed, problems)."""
    changed, problems, done = 0, [], set()
    host = [h.element for h in hits if not h.link]
    if len(host) < len(hits):
        problems.append("{} element(s) are in linked models: fix those in their own model".format(len(hits) - len(host)))
    if not host:
        return 0, problems
    tx = Transaction(doc, u"BIMPulse: set {}".format(field))
    tx.Start()
    try:
        for el in host:
            try:
                if field in NAME_FIELDS:
                    t = el if el.GetTypeId() == ElementId.InvalidElementId else doc.GetElement(el.GetTypeId())
                    target = getattr(t, "Family", None) if field == "Family name" else t
                    if target is None:
                        problems.append("{}: a system family can't be renamed".format(id_value(el.Id)))
                        continue
                    key = id_value(target.Id)
                    if key in done:
                        continue
                    done.add(key)
                    target.Name = value
                    changed += 1
                    continue
                p, owner = _find_parameter(el, field)
                if p is None:
                    near = similar_parameters(el, field)
                    problems.append(u"{}: no parameter called '{}'{}".format(
                        id_value(el.Id), field, u" (it has: {})".format(", ".join(near[:8])) if near else ""))
                    continue
                if p.IsReadOnly:
                    problems.append(u"{}: '{}' is read-only".format(id_value(el.Id), p.Definition.Name))
                    continue
                key = (id_value(owner.Id), p.Definition.Name)
                if key in done:  # a type parameter shared by several elements
                    continue
                done.add(key)
                if _set_value(p, value) is False:
                    problems.append(u"{}: Revit did not accept the value".format(id_value(el.Id)))
                    continue
                changed += 1
            except Exception as e:
                problems.append(u"{}: {}".format(id_value(el.Id), e))
        if changed:
            tx.Commit()
        else:
            tx.RollBack()
    except Exception:
        tx.RollBack()
        raise
    return changed, problems


def snapshot_png(uidoc, width=1400):
    """The active view as PNG bytes (to send with a comment)."""
    import os
    import shutil
    import tempfile
    from Autodesk.Revit.DB import (ExportRange, ImageExportOptions, ImageFileType, ImageResolution,
                                   ZoomFitType)
    folder = tempfile.mkdtemp(prefix="bimpulse-view-")
    try:
        opts = ImageExportOptions()
        opts.ExportRange = ExportRange.CurrentView
        opts.FilePath = os.path.join(folder, "view")
        opts.ZoomType = ZoomFitType.FitToPage
        opts.PixelSize = width
        opts.HLRandWFViewsFileType = ImageFileType.PNG
        opts.ShadowViewsFileType = ImageFileType.PNG
        opts.ImageResolution = ImageResolution.DPI_150
        uidoc.Document.ExportImage(opts)
        pngs = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".png")]
        if not pngs:
            raise IOError("Revit did not export the view")
        with open(max(pngs, key=os.path.getmtime), "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
