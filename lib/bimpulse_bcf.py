# -*- coding: utf-8 -*-
"""Reads BCF issue files (.bcfzip) for the BIMPulse Issues dashboard.

Plain Python that runs in pyRevit (IronPython 2.7 or 3) and in CPython (for
the tests): no Revit API here. Reads BCF 2.0, 2.1 and 3.0 from any tool, plus
the `bimpulse.json` details BIMPulse adds (audit, consultant, severity, failed
fields, and ways to find elements that have no ElementId). Review statuses and
notes are kept beside each BCF file in `<name>.bimpulse-review.json`, so the
BCF itself is never changed.
"""
from __future__ import unicode_literals

import datetime
import io
import json
import os
import re
import zipfile

try:  # CPython, and IronPython builds that ship expat.
    import xml.etree.ElementTree as ET
    ET.fromstring("<a/>")
    ParseError = ET.ParseError
except Exception:  # IronPython without expat: read the XML with .NET instead.
    ET = None
    import clr
    clr.AddReference("System.Xml.Linq")
    from System.Xml.Linq import XDocument, XText

    class ParseError(Exception):
        pass

    class _Node(object):
        """Enough of ElementTree's Element for this module, over an XElement."""

        def __init__(self, x):
            self._x = x
            self.tag = x.Name.LocalName
            texts = [n.Value for n in x.Nodes() if isinstance(n, XText)]
            self.text = "".join(texts) if texts else None

        def get(self, name, default=None):
            for a in self._x.Attributes():
                if a.Name.LocalName == name:
                    return a.Value
            return default

        def __iter__(self):
            return iter([_Node(c) for c in self._x.Elements()])

        def __len__(self):
            return len(list(self._x.Elements()))

        def iter(self):
            return [self] + [_Node(d) for d in self._x.Descendants()]

AUDIT_NAMES = {
    "geolocation": "Geolocation & Levels",
    "model-health": "Model Health",
    "uniclass": "Uniclass",
    "naming": "Object Naming",
    "clash": "Clash",
    "cobie": "COBie",
    "custom": "Custom audit",
    "bcf": "BCF",
}
SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}
# Review statuses the dashboard sets (kept beside the BCF, not in it).
OPEN, RESOLVED, IGNORED = "open", "resolved", "ignored"


def _local(tag):
    return tag.split("}")[-1] if isinstance(tag, str) or hasattr(tag, "split") else tag


def _child(elem, name):
    if elem is None:
        return None
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _children(elem, name):
    return [c for c in (elem if elem is not None else []) if _local(c.tag) == name]


def _text(elem, name=None):
    node = _child(elem, name) if name else elem
    return (node.text or "").strip() if node is not None and node.text else ""


def _iter(elem, name):
    return [e for e in elem.iter() if _local(e.tag) == name]


def _parse(data):
    """The root element of an XML file's bytes. Raises ParseError."""
    if ET is not None:
        return ET.fromstring(data)
    try:
        try:  # IronPython 2's zip reads are str holding bytes, so decode those too
            text = data.decode("utf-8-sig")
        except (AttributeError, UnicodeError):
            text = data
        return _Node(XDocument.Parse(text.lstrip("﻿")).Root)
    except Exception as e:
        raise ParseError(str(e))


def severity_from_bcf(topic_type, priority):
    """A severity for topics from other tools, from their type and priority."""
    p, t = (priority or "").lower(), (topic_type or "").lower()
    if "critical" in p:
        return "critical"
    if t in ("error", "fault", "clash") or p in ("high", "major"):
        return "error"
    if t in ("info", "information", "comment", "remark") or p in ("low", "minor"):
        return "info"
    return "warning"


def _guess_audit(title):
    t = (title or "").lower()
    for key, prefix in (("uniclass", "uniclass"), ("naming", "object naming"), ("model-health", "model health"),
                        ("clash", "clash"), ("cobie", "cobie"), ("geolocation", "geolocation")):
        if t.startswith(prefix):
            return key
    return "bcf"


_OLD_ID = re.compile(r"\(ID:?\s*([0-9]+)\)")


class Topic(object):
    """One issue."""

    def __init__(self):
        self.guid = ""
        self.title = ""
        self.description = ""
        self.topic_type = ""
        self.bcf_status = ""
        self.priority = ""
        self.labels = []
        self.created = ""
        self.author = ""
        self.index = 0
        self.audit = "bcf"
        self.audit_name = "BCF"
        self.consultant = ""
        self.severity = "warning"
        self.category = ""
        self.group = ""
        self.failures = []
        # Each: dict with element_id, unique_id, ifc_guid, category, family, type, level, parameter, value.
        self.elements = []
        self.comments = []
        self.snapshot_name = None
        self.source = None  # the BcfFile
        self.extra = {}
        # BIMPulse's stable id for the issue (the same in every export), for shared review statuses.
        self.issue_key = ""
        # Dashboard state.
        self.review = OPEN
        self.note = ""
        self.reviewed_by = ""
        self.reviewed_at = ""
        # Issues from BIMPulse: who it is assigned to, and by when ("YYYY-MM-DD").
        self.assignee = None
        self.assignee_name = ""
        self.due_on = ""

    @property
    def key(self):
        return "{}|{}".format(self.source.path if self.source else "", self.guid)

    @property
    def severity_rank(self):
        return SEVERITY_ORDER.get(self.severity, 2)

    def snapshot(self):
        """The snapshot image's bytes, or None."""
        if not self.snapshot_name or not self.source:
            return None
        try:
            with zipfile.ZipFile(self.source.path) as z:
                return z.read("{}/{}".format(self.guid, self.snapshot_name))
        except (KeyError, IOError, OSError, zipfile.BadZipfile):
            return None

    def search_text(self):
        parts = [self.title, self.description, self.category, self.group, self.audit_name, self.consultant]
        parts += [f.get("field", "") + " " + f.get("reason", "") for f in self.failures]
        for e in self.elements:
            parts += [e.get(k) or "" for k in ("element_id", "family", "type", "category", "value", "level")]
        return " ".join(p for p in parts if p).lower()


class BcfFile(object):
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.manifest = {}
        self.project = ""
        self.topics = []
        self.version = ""
        # Set when the file came from BIMPulse online: {"audit_id", "module", "consultant", "can_review"}.
        self.cloud = None

    @property
    def audit(self):
        return self.manifest.get("audit") or (self.topics[0].audit if self.topics else "bcf")


def _merge_elements(components, details):
    """Viewpoint components plus BIMPulse's element details, one entry per element."""
    out = []
    by_id = {}
    for d in details:
        e = dict(d)
        out.append(e)
        if e.get("element_id"):
            by_id[e["element_id"]] = e
        if e.get("unique_id"):
            by_id[e["unique_id"]] = e
    for c in components:
        match = by_id.get(c.get("element_id")) if c.get("element_id") else None
        if match is not None:
            if c.get("ifc_guid"):
                match.setdefault("ifc_guid", c["ifc_guid"])
            continue
        out.append(c)
    return out


def _components(viewpoint):
    comps = []
    for comp in _iter(viewpoint, "Component"):
        tool_id = comp.get("AuthoringToolId") or _text(comp, "AuthoringToolId")
        entry = {}
        if tool_id and tool_id.strip().isdigit():
            entry["element_id"] = tool_id.strip()
        if comp.get("IfcGuid"):
            entry["ifc_guid"] = comp.get("IfcGuid")
        system = comp.get("OriginatingSystem") or _text(comp, "OriginatingSystem")
        if system:
            entry["system"] = system
        if entry.get("element_id") or entry.get("ifc_guid"):
            comps.append(entry)
    return comps


def _viewpoint_files(markup, folder_files):
    """(viewpoint file, snapshot file) of the topic's first viewpoint (BCF 2.x or 3.0)."""
    for vp in _iter(markup, "Viewpoints") + _iter(markup, "ViewPoint"):
        view, snap = _text(vp, "Viewpoint"), _text(vp, "Snapshot")
        if view or snap:
            return view or None, snap or None
    view = "viewpoint.bcfv" if "viewpoint.bcfv" in folder_files else None
    snap = next((f for f in ("snapshot.png", "snapshot.jpg") if f in folder_files), None)
    return view, snap


def load(path):
    """A BcfFile with every topic, or raises ValueError if it isn't a BCF zip."""
    bcf = BcfFile(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipfile, IOError, OSError):
        raise ValueError("Not a BCF file: {}".format(os.path.basename(path)))
    with z:
        names = z.namelist()
        if "bcf.version" in names:
            try:
                bcf.version = _parse(z.read("bcf.version")).get("VersionId") or ""
            except ParseError:
                pass
        if "bimpulse.json" in names:
            try:
                bcf.manifest = json.loads(z.read("bimpulse.json").decode("utf-8"))
            except ValueError:
                bcf.manifest = {}
        for proj in ("project.bcfp", "project.bcfp.xml"):
            if proj in names:
                try:
                    bcf.project = _text(_child(_parse(z.read(proj)), "Project"), "Name")
                except ParseError:
                    pass
        folders = {}
        for n in names:
            if "/" in n and not n.endswith("/"):
                folder, fname = n.split("/", 1)
                folders.setdefault(folder, []).append(fname)
        for folder, files in folders.items():
            if "markup.bcf" not in files:
                continue
            try:
                topic = _read_topic(z, folder, files, bcf)
            except ParseError:
                continue
            bcf.topics.append(topic)
    if not bcf.topics and "bcf.version" not in names:
        raise ValueError("Not a BCF file: {}".format(os.path.basename(path)))
    bcf.topics.sort(key=lambda t: (t.index or 10 ** 9, t.title))
    _load_review(bcf)
    return bcf


def _read_topic(z, folder, files, bcf):
    markup = _parse(z.read("{}/markup.bcf".format(folder)))
    t = Topic()
    t.source = bcf
    node = _child(markup, "Topic")
    t.guid = (node.get("Guid") if node is not None else None) or folder
    t.title = _text(node, "Title")
    t.description = _text(node, "Description")
    t.topic_type = (node.get("TopicType") if node is not None else "") or _text(node, "TopicType")
    t.bcf_status = (node.get("TopicStatus") if node is not None else "") or _text(node, "TopicStatus")
    t.priority = _text(node, "Priority")
    t.labels = [_text(lb) for lb in _children(node, "Labels") if _text(lb)]
    labels_node = _child(node, "Labels")
    if labels_node is not None and len(labels_node):  # BCF 3.0: <Labels><Label/></Labels>
        t.labels = [_text(lb) for lb in _children(labels_node, "Label") if _text(lb)]
    t.created = _text(node, "CreationDate")
    t.author = _text(node, "CreationAuthor")
    try:
        t.index = int(_text(node, "Index") or 0)
    except ValueError:
        t.index = 0
    t.comments = [{"text": _text(c, "Comment"), "author": _text(c, "Author"), "date": _text(c, "Date")}
                  for c in _iter(markup, "Comment") if _text(c, "Comment")]
    view_file, t.snapshot_name = _viewpoint_files(markup, files)
    if t.snapshot_name and t.snapshot_name not in files:
        t.snapshot_name = None
    components = []
    if view_file and view_file in files:
        try:
            components = _components(_parse(z.read("{}/{}".format(folder, view_file))))
        except ParseError:
            components = []
    details = {}
    if "bimpulse.json" in files:
        try:
            details = json.loads(z.read("{}/bimpulse.json".format(folder)).decode("utf-8"))
        except ValueError:
            details = {}
    manifest = bcf.manifest
    t.audit = details.get("audit") or manifest.get("audit") or _guess_audit(t.title)
    t.audit_name = manifest.get("audit_name") if manifest.get("audit") == t.audit and manifest.get("audit_name") \
        else AUDIT_NAMES.get(t.audit, t.audit)
    t.consultant = details.get("consultant") or manifest.get("consultant") or ""
    t.severity = details.get("severity") or severity_from_bcf(t.topic_type, t.priority)
    t.category = details.get("category") or ""
    t.group = details.get("group") or ""
    t.failures = list(details.get("failures") or [])
    t.extra = details.get("extra") or {}
    t.issue_key = details.get("key") or ""
    t.elements = _merge_elements(components, details.get("elements") or [])
    if not t.elements and t.audit == "uniclass":  # BIMPulse's first BCF exports: the id is in the title
        m = _OLD_ID.search(t.title)
        if m:
            t.elements = [{"element_id": m.group(1)}]
    if not t.failures and t.description:
        t.failures = [{"field": "", "reason": line.strip(" -")} for line in t.description.splitlines() if line.strip(" -")]
    if t.bcf_status.lower() in ("closed", "resolved", "done"):
        t.review = RESOLVED
    return t


# --- review status, kept beside the BCF ---

def review_path(bcf_path):
    root, _ = os.path.splitext(bcf_path)
    return root + ".bimpulse-review.json"


def _load_review(bcf):
    path = review_path(bcf.path)
    if not os.path.exists(path):
        return
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, IOError, OSError):
        return
    for t in bcf.topics:
        r = (data.get("topics") or {}).get(t.guid)
        if r:
            t.review = r.get("status") or t.review
            t.note = r.get("note") or ""
            t.reviewed_by = r.get("by") or ""
            t.reviewed_at = r.get("at") or ""


def save_review(bcf):
    """Writes every topic's review status and note beside the BCF. Returns the path."""
    data = {"bcf": bcf.name, "saved": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "topics": {}}
    for t in bcf.topics:
        if t.review != OPEN or t.note:
            data["topics"][t.guid] = {"status": t.review, "note": t.note, "by": t.reviewed_by, "at": t.reviewed_at,
                                      "title": t.title}
    path = review_path(bcf.path)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, type("")) else text.decode("utf-8"))
    return path


def find_bcf_files(folder):
    """Every .bcfzip / .bcf file in a folder (not its subfolders), newest first."""
    out = []
    for name in os.listdir(folder):
        if name.lower().endswith((".bcfzip", ".bcf")):
            out.append(os.path.join(folder, name))
    return sorted(out, key=lambda p: os.path.getmtime(p), reverse=True)


def csv_rows(topics):
    """Rows for the "Export review" CSV: header first."""
    rows = [["Audit", "Consultant", "Severity", "Title", "Group", "Elements", "Review", "Note", "Reviewed by",
             "Reviewed at", "BCF file"]]
    for t in topics:
        ids = ", ".join(e.get("element_id") or e.get("unique_id") or "" for e in t.elements if
                        e.get("element_id") or e.get("unique_id"))
        rows.append([t.audit_name, t.consultant, t.severity.title(), t.title, t.group, ids, t.review.title(),
                     t.note, t.reviewed_by, t.reviewed_at, t.source.name if t.source else ""])
    return rows


def csv_text(rows):
    """Rows as CSV text (Excel-friendly quoting), without the csv module's
    IronPython 2 unicode problems."""
    def cell(v):
        s = "" if v is None else "{}".format(v)
        if any(c in s for c in ',"\r\n'):
            s = '"' + s.replace('"', '""') + '"'
        return s
    return "\r\n".join(",".join(cell(v) for v in row) for row in rows) + "\r\n"


# --- Revit UniqueId <-> IFC GUID (as bimpulse_engine.bcf), for BCFs from other tools ---

_IFC_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"
_UNIQUE_ID = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})-([0-9a-fA-F]{8})$")


def ifc_guid_from_unique_id(unique_id):
    """The IFC GUID Revit's IFC exporter gives an element, or None."""
    m = _UNIQUE_ID.match((unique_id or "").strip())
    if not m:
        return None
    episode, element = m.group(1), int(m.group(2), 16)
    last = int(episode[-8:], 16) ^ element
    n = int((episode[:-8] + "{:08x}".format(last)).replace("-", ""), 16)
    chars = []
    for _ in range(22):
        chars.append(_IFC_CHARS[n & 63])
        n >>= 6
    return "".join(reversed(chars))
