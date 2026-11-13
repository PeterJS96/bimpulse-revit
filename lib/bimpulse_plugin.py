# -*- coding: utf-8 -*-
"""Plain helpers for the BIMPulse Dashboard (no Revit, no WPF, so CPython tests run them):
the light and dark themes, which issues are "similar" for fixing many at once, the
first-run tour's steps, and cleaning error reports of personal paths.
"""
from __future__ import unicode_literals

import os
import re

# The dashboard is designed dark (BIMPulse's own colours); light swaps each colour for its
# counterpart. Keys are upper-case #RRGGBB as used in the XAML and the code.
LIGHT = {
    "#050B14": "#F3F6FA",  # page
    "#0B1626": "#FFFFFF",  # cards, menus
    "#10203A": "#EEF3F8",  # buttons, chips
    "#081221": "#F7F9FC",  # fields, rows
    "#0E2236": "#E6F4FB",  # selected row
    "#14304D": "#D6EEF9",  # highlighted
    "#16243A": "#E3E9F0",  # faint lines
    "#1C2B40": "#D3DCE6",  # lines
    "#22344F": "#C2CDD9",  # scroll bars
    "#2A3D57": "#A9B6C6",  # check boxes
    "#2A4A6E": "#8FC3E3",  # hover
    "#E6EEF7": "#0F1B2A",  # text
    "#C9D6E6": "#2C3B4E",  # description
    "#8A9BB3": "#566579",  # muted text
    "#5D6F88": "#7A8899",  # faint text
    "#00A0E3": "#0087C2",  # BIMPulse blue
    "#03101C": "#FFFFFF",  # text on blue
    "#22C55E": "#16A34A",
    "#F59E0B": "#D97706",
    "#F97316": "#EA580C",
    "#EF4444": "#DC2626",
}
THEMES = ("auto", "light", "dark")
_HEX6 = re.compile(r"(?<![0-9A-Fa-f])#([0-9A-Fa-f]{6})(?![0-9A-Fa-f])")


def theme_for(setting, revit_theme):
    """'light' or 'dark': the person's choice, or Revit's own theme ('auto')."""
    if setting in ("light", "dark"):
        return setting
    return "dark" if revit_theme == "dark" else "light"


def colour(hex_colour, theme):
    """A colour (#RRGGBB or #AARRGGBB) in the theme."""
    if theme != "light" or not hex_colour or not hex_colour.startswith("#"):
        return hex_colour
    if len(hex_colour) == 9:
        return "#" + hex_colour[1:3] + LIGHT.get("#" + hex_colour[3:].upper(), "#" + hex_colour[3:])[1:]
    return LIGHT.get(hex_colour.upper(), hex_colour)


def themed_xaml(text, theme, styles="", logo="", fonts=("Segoe UI", "Consolas")):
    """The window's XAML in the theme, with the shared styles put in for <!--BIMPULSE:STYLES-->,
    the BIMPulse mark's file for BIMPULSE:LOGO and the text and mono fonts (the website's Geist
    and Geist Mono) for BIMPULSE:FONT and BIMPULSE:MONO."""
    text = text.replace("<!--BIMPULSE:STYLES-->", styles).replace("BIMPULSE:LOGO", logo)
    text = text.replace("BIMPULSE:FONT", fonts[0]).replace("BIMPULSE:MONO", fonts[1])
    if theme != "light":
        return text
    return _HEX6.sub(lambda m: colour("#" + m.group(1), theme), text)


# ------------------------------------------------------------------ fixing many at once

def failure_on(topic, field):
    return next((f for f in topic.failures if f.get("field") == field and "value" in f), None)


def _kind(topic):
    """What the issue's elements are: the first element's category, family and type."""
    e = topic.elements[0] if topic.elements else {}
    return (e.get("category") or "", e.get("family") or "", e.get("type") or "")


def similar_issues(topics, topic, field):
    """Other open issues that fail `field` the same way as `topic` on the same kind of element
    (same category, family and type; same value now and the same suggestion), in its result."""
    mine = failure_on(topic, field)
    if mine is None:
        return []
    out = []
    for t in topics:
        if t is topic or t.review != "open" or not t.elements or t.source is not topic.source:
            continue
        f = failure_on(t, field)
        if f is None or (f.get("value") or "") != (mine.get("value") or "") \
                or (f.get("suggestion") or "") != (mine.get("suggestion") or "") or _kind(t) != _kind(topic):
            continue
        out.append(t)
    return out


# ------------------------------------------------------------------ error reports

def scrub(text):
    """An error report's text without the person's own paths (their Windows user name)."""
    text = text or ""
    for var in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"):
        value = os.environ.get(var)
        if value and len(value) > 3:
            text = text.replace(value, "%" + var + "%")
    text = re.sub(r"(?i)([A-Z]:\\Users\\)[^\\\"']+", r"\1<user>", text)
    text = re.sub(r"(?i)\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "<email>", text)
    return text


def error_report(where, error, detail, versions):
    """The POST /revit/error-reports body (no model data, file names or e-mail addresses)."""
    body = {"where": (where or "?")[:120], "error": scrub(str(error))[:500] or "Unknown error",
            "detail": scrub(detail)[-8000:] or None}
    for k in ("plugin_version", "revit_version", "pyrevit_version"):
        if versions.get(k):
            body[k] = str(versions[k])[:60]
    return body


# ------------------------------------------------------------------ first-run tour

TOUR = [
    ("btnBrowse", "Open your audits",
     "Browse BIMPulse lists your projects and saved audits. Tick the results you want and their issues open here."),
    ("cardList", "Your issues",
     "Search, filter and sort. Click an issue to select its elements in the model; Ctrl+Up and Ctrl+Down step through."),
    ("cardDetail", "Fix it in the model",
     "Set a missing or wrong value straight from here, on one issue or on every similar one, and it is logged in "
     "BIMPulse."),
    ("cardDetail", "Talk with your team",
     "Comments and statuses are shared live with the BIMPulse Issues page. Tick 'Attach this view' to send a picture "
     "and the view itself, so anyone can go straight back to it."),
    ("btnColour", "See them in context",
     "Colour every open issue in the active view by severity. 'Clear colours' puts the view back."),
    ("btnMenu", "Make it yours",
     "Light or dark, dock it beside the model, error reports, and this tour again, all from this menu."),
]


def swap_map(old, new):
    """{colour: colour} to repaint something drawn in the `old` theme in the `new` one."""
    if old == new:
        return {}
    if new == "light":
        return dict(LIGHT)
    back = {}
    for dark, light in LIGHT.items():
        back.setdefault(light, dark)
    back["#FFFFFF"] = "#0B1626"  # white was cards (and text on blue): back to cards
    return back


def in_selection(topic, ids, unique_ids):
    """Whether one of the issue's elements is selected in Revit: by its unique id when the issue
    has one (safe across models), else by its element id."""
    for e in topic.elements:
        if e.get("unique_id"):
            if e["unique_id"] in unique_ids:
                return True
        elif e.get("element_id") and str(e["element_id"]) in ids:
            return True
    return False


# ------------------------------------------------------------------ health, biggest wins, recent

def grade(score):
    """BIMPulse's bands for a score (%): Green, Amber or Red."""
    if score is None:
        return ""
    return "Green" if score >= 80 else ("Amber" if score >= 50 else "Red")


def average(scores):
    scores = [float(s) for s in scores if s is not None]
    return int(round(sum(scores) / len(scores))) if scores else None


def _what(topic):
    e = topic.elements[0] if topic.elements else {}
    return e.get("category") or topic.category or "elements"


def biggest_wins(topics, not_fixable=(), limit=4):
    """The few changes that clear the most open issues: the same value to set on the same kind of
    element first, then the biggest groups of the same check. Each: {title, detail, keys, fix}."""
    fixes, groups = {}, {}
    for t in topics:
        if t.review != "open":
            continue
        fixable = [f for f in t.failures if t.elements and "value" in f and f.get("field")
                   and f["field"] not in not_fixable]
        for f in fixable:
            k = (id(t.source), f["field"], f.get("suggestion") or "", f.get("value") or "", _what(t))
            fixes.setdefault(k, []).append(t)
        if not fixable:
            groups.setdefault((t.audit_name, t.group or t.category or t.title), []).append(t)
    out = []
    for (_, field, suggestion, value, what), ts in fixes.items():
        if len(ts) < 2:
            continue
        if suggestion:
            title = u"Set {} to “{}” on {} {}".format(field, suggestion, len(ts), what)
        elif not value:
            title = u"Fill in {} on {} {}".format(field, len(ts), what)
        else:
            title = u"Fix {} (now “{}”) on {} {}".format(field, value, len(ts), what)
        out.append({"title": title, "detail": u"{} issues · {} · fix them all at once".format(
            len(ts), ts[0].audit_name), "keys": [t.key for t in ts], "fix": True, "count": len(ts)})
    for (audit, group), ts in groups.items():
        if len(ts) < 2:
            continue
        out.append({"title": u"{} {} issues".format(len(ts), group), "detail": u"{} · work through them together".format(
            audit), "keys": [t.key for t in ts], "fix": False, "count": len(ts)})
    out.sort(key=lambda w: (not w["fix"], -w["count"]))
    return out[:limit]


def remember_recent(settings, account, entry, limit=6):
    """Puts a just-opened audit first in this account's recent list (one per audit)."""
    recent = [r for r in settings.get("recent") or []
              if not (r.get("account") == account and r.get("audit_id") == entry["audit_id"])]
    entry = dict(entry, account=account)
    settings["recent"] = ([entry] + recent)[:limit * 3]
    return [r for r in settings["recent"] if r.get("account") == account][:limit]


def recent_for(settings, account, limit=6):
    return [r for r in settings.get("recent") or [] if r.get("account") == account][:limit]
