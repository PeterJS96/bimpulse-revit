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


def themed_xaml(text, theme, styles=""):
    """The window's XAML in the theme, with the shared styles put in for <!--BIMPULSE:STYLES-->."""
    text = text.replace("<!--BIMPULSE:STYLES-->", styles)
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
