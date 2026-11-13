# -*- coding: utf-8 -*-
"""The BIMPulse Dashboard: your BIMPulse audit issues in Revit. Opens a saved audit's issues
(BCF from BIMPulse), filters and searches them, selects, zooms, isolates or shows their
elements in 3D, colours them by severity, fixes values in the model (one issue or every
similar one), and shares statuses and comments (with the Revit view) live with the
BIMPulse Issues page.

One class (Dashboard) drives two hosts with the same controls: a window (window.xaml, the
Dashboard button) and a panel docked beside the model (panel.xaml, registered when Revit
starts by the extension's startup.py). The look follows Revit's light or dark theme, or the
person's choice in the menu.
"""
import datetime
import io
import json
import os
import tempfile
import threading
import traceback

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
import System
from System.IO import MemoryStream
from System.Windows import (Clipboard, CornerRadius, FontWeights, GridLength, GridUnitType, HorizontalAlignment, Point,
                            Rect, Size, SystemParameters, TextTrimming, TextWrapping, Thickness, VerticalAlignment,
                            Visibility, WindowState)
from System.Windows.Controls import (Border, Button, CheckBox, ColumnDefinition, ComboBox, ContextMenu, Grid, Image,
                                     ListBoxItem, MenuItem, Orientation, Separator, StackPanel, TextBlock, TextBox)
from System.Windows.Controls.Primitives import PlacementMode
from System.Windows.Input import Cursors, Key, Keyboard, ModifierKeys
from System.Windows.Media import (BrushConverter, CombinedGeometry, FontFamily, GeometryCombineMode,
                                  RectangleGeometry, SolidColorBrush, VisualTreeHelper)
from System.Windows.Media.Imaging import BitmapCacheOption, BitmapImage
from System.Windows.Threading import DispatcherTimer
from System.Windows import Duration, UIElement
from System.Windows.Media import TranslateTransform
from System.Windows.Media.Animation import CubicEase, DoubleAnimation, EasingMode, RepeatBehavior
from System.Windows.Media import ArcSegment, PathFigure, PathGeometry, ScaleTransform, SweepDirection
from System.Windows.Shapes import Ellipse, Path as ShapePath
from System.Windows import DependencyProperty
from System.Windows.Media import BrushMappingMode, Color, GradientStop, RadialGradientBrush
from System.Windows.Media.Effects import DropShadowEffect

from Autodesk.Revit.UI import DockablePaneId, ExternalEvent, IExternalEventHandler
from pyrevit import HOST_APP, forms

import bimpulse_bcf as bcf
import bimpulse_cloud as cloud
import bimpulse_markup as markup
import bimpulse_plugin as plug
import bimpulse_revit as rv

EXTENSION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bimpulse_ui")
# The docked panel's id (Revit remembers where it was docked by this).
PANEL_ID = "6f3c2a91-4b7e-4f0d-9c55-2e8a7d1b3c40"
REQUEST_SECONDS = 6  # how often BIMPulse is asked for "Open in Revit" requests
THEME = {"name": "dark"}  # the theme the open dashboards were drawn in
# Single-key shortcuts (not while typing): key, button, what it does.
SHORTCUTS = (("Z", "btnZoom", "Select and zoom"), ("D", "btn3D", "Show in 3D"), ("S", "btnSelect", "Select only"),
             ("X", "btnIsolate", "Isolate"), ("R", "btnResolve", "Mark resolved"), ("I", "btnIgnore", "Ignore"),
             ("O", "btnReopen", "Reopen"), ("C", "btnColour", "Colour issues in the view"),
             ("B", "btnBrowse", "Browse BIMPulse"))


def _code_stamp():
    try:
        return os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return None


# When this code was loaded: after an update, a dashboard still running older code (the docked
# panel keeps its code until Revit restarts) leaves "Open in Revit" requests to the new code.
LOADED = _code_stamp()


def outdated():
    now = _code_stamp()
    return LOADED is not None and now is not None and now != LOADED

SEV_COLOUR = {"critical": "#EF4444", "error": "#F97316", "warning": "#F59E0B", "info": "#00A0E3"}
SEV_LABEL = {"critical": "CRITICAL", "error": "ERROR", "warning": "WARNING", "info": "INFO"}
REVIEW_COLOUR = {bcf.OPEN: "#F59E0B", bcf.RESOLVED: "#22C55E", bcf.IGNORED: "#8A9BB3"}
ROW_STATUS = {"found": ("Found", "#22C55E"), "link": ("In a link", "#00A0E3"), "many": ("Matched", "#00A0E3"),
              "mismatch": ("Different element", "#F59E0B"), "missing": ("Not in this model", "#EF4444")}
LIST_LIMIT = 1500
LIVE_SECONDS = 20  # how often BIMPulse is asked for new comments and statuses
IGNORE_NOTE = "Type why it is ignored in the comment box first: BIMPulse keeps the reason with the issue."
# Failed fields that are not values in the model, so they can't be fixed from here.
NOT_FIXABLE = ("Clash", "Warning", "Level", "Name")
VIEWER_NOTE = "Viewers can see issue statuses but not change them. Ask an administrator for the Auditor role."
SETTINGS = os.path.join(os.environ.get("APPDATA", tempfile.gettempdir()), "BIMPulse", "issues-dashboard.json")
UI_CACHE = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "BIMPulse", "ui")

DEBUG_LOG = os.path.join(os.environ.get("APPDATA", tempfile.gettempdir()), "BIMPulse", "issues-debug.log")


def debug(message):
    """Appends a line to %APPDATA%\\BIMPulse\\issues-debug.log (what happened, for support)."""
    try:
        folder = os.path.dirname(DEBUG_LOG)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(u"{} {}\n".format(datetime.datetime.now().strftime("%H:%M:%S"), message))
    except Exception:
        pass


_brushes = {}


def brush(hex_colour):
    """A frozen brush for a colour of the dark palette, in the dashboard's theme."""
    shown = plug.colour(hex_colour, THEME["name"])
    if shown not in _brushes:
        b = BrushConverter().ConvertFromString(shown)
        b.Freeze()
        _brushes[shown] = b
    return _brushes[shown]


ICON_FONT = FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets")
# The website's fonts, shipped with the plugin (SIL Open Font Licence: bimpulse_ui/fonts/OFL.txt),
# with Windows' own as the fallback.
_FONTS = System.Uri(os.path.join(UI_DIR, "fonts") + os.sep).AbsoluteUri
SANS = _FONTS + "#Geist, Segoe UI"
MONO = _FONTS + "#Geist Mono, Consolas"
SANS_FONT = FontFamily(SANS)
MONO_FONT = FontFamily(MONO)
DISPLAY_FONT = SANS_FONT
# Buttons and their icons (Segoe icon font): (name, glyph, icon after the text).
# In the docked panel these buttons show only their icon (with a tooltip), so the actions fit.
ICON_ONLY_DOCKED = ("btnSelect", "btnProps", "btnIsolate", "btnPrev", "btnNext", "btnReload", "btnUndoFix", "btnReopen")
ICONS = (("btnBrowse", u"\uE838", False), ("btnReload", u"\uE72C", False), ("btnCheck", u"\uE9D5", False),
         ("btnZoom", u"\uE71E", False), ("btn3D", u"\uE81E", False), ("btnIsolate", u"\uE71C", False),
         ("btnSelect", u"\uE8B3", False), ("btnProps", u"\uE8C8", False), ("btnResolve", u"\uE73E", False),
         ("btnIgnore", u"\uED1A", False), ("btnReopen", u"\uE7A7", False), ("btnPrev", u"\uE76B", False),
         ("btnNext", u"\uE76C", True), ("btnColour", u"\uE790", False), ("btnClear", u"\uE894", False),
         ("btnSend", u"\uE724", False), ("btnUndoFix", u"\uE7A7", False), ("btnMenu", u"\uE712", False),
         ("btnCommand", u"\uE721", False), ("btnUpdateNow", u"\uE896", False), ("btnAssign", u"\uE74E", False),
         ("btnBrowseOpen", u"\uE8E5", False))
# What the command bar can run: (button, title, words it is found by).
COMMANDS = (("btnZoom", "Select & zoom", "zoom find select"), ("btn3D", "Show in 3D", "3d section box view"),
            ("btnIsolate", "Isolate in view", "isolate hide others"), ("btnSelect", "Select only", "select"),
            ("btnResolve", "Mark resolved", "resolve done fixed close"), ("btnIgnore", "Ignore", "ignore"),
            ("btnReopen", "Reopen", "reopen open"), ("btnUndoFix", "Undo the last fix", "undo fix back"),
            ("btnColour", "Colour issues in the view", "colour color severity"),
            ("btnClear", "Clear colours and isolation", "clear colours reset"),
            ("btnBrowse", "Browse BIMPulse", "browse open audit project"), ("btnReload", "Reload issues", "reload refresh"),
            ("btnCheck", "Check against the model", "check model found"))


def _reduced_motion():
    try:
        return not SystemParameters.ClientAreaAnimation
    except Exception:
        return False


def animate_in(el, dy=8.0, ms=180, delay=0):
    """Fades and slides an element in (skipped when Windows animations are off)."""
    if el is None or _reduced_motion():
        return
    ease = CubicEase()
    ease.EasingMode = EasingMode.EaseOut
    move = TranslateTransform(0, dy)
    el.RenderTransform = move
    fade = DoubleAnimation(0.0, 1.0, Duration(System.TimeSpan.FromMilliseconds(ms)))
    slide = DoubleAnimation(dy, 0.0, Duration(System.TimeSpan.FromMilliseconds(ms)))
    for a in (fade, slide):
        a.EasingFunction = ease
        a.BeginTime = System.TimeSpan.FromMilliseconds(delay)
    el.Opacity = 0
    el.BeginAnimation(UIElement.OpacityProperty, fade)
    move.BeginAnimation(TranslateTransform.YProperty, slide)


def skeleton(rows=6, height=56, gap=6):
    """Grey placeholder rows that gently pulse while something loads."""
    sp = StackPanel()
    for i in range(rows):
        b = Border()
        b.Height = height
        b.CornerRadius = CornerRadius(10)
        b.Margin = Thickness(0, 0, 6, gap)
        b.Background = brush("#10203A")
        if not _reduced_motion():
            pulse = DoubleAnimation(1.0, 0.45, Duration(System.TimeSpan.FromMilliseconds(750)))
            pulse.AutoReverse = True
            pulse.RepeatBehavior = RepeatBehavior.Forever
            pulse.BeginTime = System.TimeSpan.FromMilliseconds(i * 90)
            b.BeginAnimation(UIElement.OpacityProperty, pulse)
        sp.Children.Add(b)
    return sp


def _mix(a, b, t):
    """Colour a moved t of the way towards b."""
    return Color.FromArgb(255, int(a.R + (b.R - a.R) * t), int(a.G + (b.G - a.G) * t), int(a.B + (b.B - a.B) * t))


def hover_card(title, rows, hint=None):
    """A rich tooltip: a title, label/value rows and a hint (styled by the shared ToolTip style)."""
    sp = StackPanel()
    sp.Children.Add(text(title, 12.5, bold=True))
    for label, value in rows:
        if value in (None, ""):
            continue
        line = StackPanel()
        line.Orientation = Orientation.Horizontal
        line.Margin = Thickness(0, 4, 0, 0)
        if label:
            k = text(label.upper(), 10, "#8A9BB3", mono=True)
            k.MinWidth = 84
            k.VerticalAlignment = VerticalAlignment.Center
            line.Children.Add(k)
        line.Children.Add(text(u"{}".format(value), 12, wrap=True))
        sp.Children.Add(line)
    if hint:
        sp.Children.Add(text(hint, 11, "#00A0E3", margin=(0, 7, 0, 0), wrap=True))
    return sp


def ago(iso):
    """ "2026-11-10T09:00:00" -> "2 days ago" (roughly). """
    try:
        then = datetime.datetime.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    secs = (datetime.datetime.utcnow() - then).total_seconds()
    for size, unit in ((86400 * 30, "month"), (86400 * 7, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = int(secs // size)
            return u"{} {}{} ago".format(n, unit, "" if n == 1 else "s")
    return "just now"


def revit_theme():
    """'dark' when Revit's own theme is dark (Revit 2024 and later), else 'light'."""
    try:
        from Autodesk.Revit.UI import UITheme, UIThemeManager
        return "dark" if UIThemeManager.CurrentTheme == UITheme.Dark else "light"
    except Exception:
        return "light"  # Revit 2023 and earlier are always light


def current_theme(settings=None):
    return plug.theme_for((settings if settings is not None else load_settings()).get("theme", "auto"), revit_theme())


def themed_xaml(name, theme):
    """Writes the window's or panel's XAML in the theme (with the shared styles) to the local
    cache and returns its path."""
    with io.open(os.path.join(UI_DIR, name), encoding="utf-8") as f:
        text = f.read()
    with io.open(os.path.join(UI_DIR, "styles.part.xaml"), encoding="utf-8") as f:
        styles = f.read()
    if not os.path.isdir(UI_CACHE):
        os.makedirs(UI_CACHE)
    path = os.path.join(UI_CACHE, "{}-{}.xaml".format(os.path.splitext(name)[0], theme))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(plug.themed_xaml(text, theme, styles, os.path.join(UI_DIR, "logo.png"), (SANS, MONO)))
    THEME["name"] = theme
    return path


def _username():
    try:
        return HOST_APP.username
    except Exception:
        return ""


def versions():
    """The plugin's, Revit's and pyRevit's versions (for error reports and the menu)."""
    out = {"plugin_version": (cloud.installed_version(EXTENSION_DIR) or "dev")[:12]}
    try:
        out["revit_version"] = "{} ({})".format(HOST_APP.version, HOST_APP.build)
    except Exception:
        pass
    try:
        from pyrevit import versionmgr
        out["pyrevit_version"] = versionmgr.get_pyrevit_version().get_formatted()
    except Exception:
        pass
    return out


def text(value, size=13, colour="#E6EEF7", bold=False, mono=False, wrap=False, margin=None):
    tb = TextBlock()
    tb.Text = value or ""
    tb.FontSize = size
    tb.Foreground = brush(colour)
    if bold:
        tb.FontWeight = FontWeights.SemiBold
    if mono:
        tb.FontFamily = MONO_FONT
    if wrap:
        tb.TextWrapping = TextWrapping.Wrap
    else:
        tb.TextTrimming = TextTrimming.CharacterEllipsis
    if margin:
        tb.Margin = Thickness(*margin)
    return tb


def pill(label, colour):
    b = Border()
    b.CornerRadius = CornerRadius(10)
    b.Padding = Thickness(8, 2, 8, 2)
    b.Background = brush("#26" + colour[1:])
    b.Child = text(label, 10, colour, bold=True, mono=True)
    b.VerticalAlignment = VerticalAlignment.Center
    return b


def initials(name):
    words = [w for w in (name or "").replace(".", " ").replace("@", " ").split() if w]
    return "".join(w[0].upper() for w in words[:2]) or "?"


def short_date(day):
    """ "2026-11-20" -> "20 Nov" """
    try:
        d = datetime.datetime.strptime(day[:10], "%Y-%m-%d")
        return "{} {}".format(d.day, d.strftime("%b"))
    except (TypeError, ValueError):
        return day or ""


def uniclass_prefix(field):
    """ "Uniclass Pr" -> "Pr" (a code field that can be searched), else None. """
    parts = (field or "").split()
    if len(parts) == 2 and parts[0] == "Uniclass" and parts[1] in ("Pr", "Ss", "EF"):
        return parts[1]
    return None


def load_settings():
    try:
        with io.open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        folder = os.path.dirname(SETTINGS)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        text_ = json.dumps(data, indent=2, ensure_ascii=False)
        with io.open(SETTINGS, "w", encoding="utf-8") as f:
            f.write(text_ if isinstance(text_, type(u"")) else text_.decode("utf-8"))
    except Exception:
        pass


class RevitQueue(IExternalEventHandler):
    """Runs queued functions in Revit's API context (the window is modeless)."""

    def __init__(self):
        self.jobs = []
        self.on_error = None

    def Execute(self, uiapp):
        while self.jobs:
            job = self.jobs.pop(0)
            try:
                job(uiapp)
            except Exception as e:
                debug(u"Revit job failed: {}".format(traceback.format_exc()))
                if self.on_error:
                    self.on_error(e)

    def GetName(self):
        return "BIMPulse Dashboard"


class Dashboard(object):
    """The dashboard's behaviour, mixed into the window and the docked panel (their XAML names
    the same controls). setup() wires it up; start() opens the last results and goes live."""

    def setup(self, docked=False):
        self.docked = docked
        self.started = False
        self.tour_step = None
        self._asked_reports = False
        self._latest = None  # the newest published plugin commit, when it differs from this one
        self.files = []
        self.current = None
        self.audit_filter = None
        self.finders = {}
        self.coloured = {}  # view id -> [ElementId] coloured by this window
        self.settings = load_settings()
        self.username = _username()
        self.session = cloud.Session.load()
        self.signin = None
        self.me = None
        self.projects = []
        self.browse_audit = None
        self.since = {}  # audit id -> newest activity seen
        self.people = []  # the organisation's members, for assigning issues
        self.seen = cloud.Seen()  # which conversations have news since this user last looked
        self.outbox = cloud.Outbox()  # changes made offline, sent when BIMPulse can be reached
        self.flushing = False
        self.fixed = {}  # topic key -> fields fixed in the model this session
        self.last_fix = None  # what the last fix changed, for "Undo fix"
        self.win_filter = None  # (issue keys, label): a biggest win or "still failing" being worked through
        self._last_pct = None
        self.selection = None  # (element ids, unique ids) when showing only the selection's issues
        self._selection_handler = None
        self.images = {}  # attachment path -> PNG/JPEG bytes
        self._assignee_ids = [None]  # cbAssignee's people, by position
        self.polling = False
        self.timer = DispatcherTimer()
        self.timer.Interval = System.TimeSpan.FromSeconds(LIVE_SECONDS)
        self.timer.Tick += lambda s, a: self.poll_activity()
        self.requests = DispatcherTimer()
        self.requests.Interval = System.TimeSpan.FromSeconds(REQUEST_SECONDS)
        self.requests.Tick += lambda s, a: self.poll_requests()
        self.asking = False
        # This Dashboard, so "Open in Revit" on the web can be sent to it (checked in every 3rd tick).
        import uuid
        self.session_id = str(uuid.uuid4())
        self._ticks = 0

        self.queue = RevitQueue()
        self.queue.on_error = lambda e: self.status("Revit: {}".format(e), error=True)
        self.event = ExternalEvent.Create(self.queue)

        for cb, items in ((self.cbConsultant, ["All consultants"]),
                          (self.cbSeverity, ["All severities", "Critical", "Error", "Warning", "Info"]),
                          (self.cbReview, ["Open issues", "All statuses", "Resolved", "Ignored", "Assigned to me",
                                           "Overdue"]),
                          (self.cbSort, ["Sort: severity", "Sort: audit", "Sort: title", "Sort: elements"])):
            for i in items:
                cb.Items.Add(i)
            cb.SelectedIndex = 0
            cb.SelectionChanged += self.on_filter

        self.btnReload.Click += lambda s, a: self.load(list(f.path for f in self.files), self._tags())
        self.btnBrowse.Click += self.on_browse
        self.btnAccount.Click += self.on_account
        self.btnBrowseClose.Click += lambda s, a: self.show_browse(False)
        self.btnBrowseOpen.Click += self.on_browse_open
        self.btnSelectAll.MouseLeftButtonUp += lambda s, a: self._tick_all(True)
        self.btnSelectNone.MouseLeftButtonUp += lambda s, a: self._tick_all(False)
        self.tbProjectSearch.TextChanged += self.on_project_search
        self.lbProjects.SelectionChanged += self.on_project_pick
        self.cbRevision.SelectionChanged += self.on_revision_pick
        self.btnCheck.Click += self.on_check
        self.tbSearch.TextChanged += self.on_search
        self.btnClearFilters.MouseLeftButtonUp += self.on_clear_filters
        self.lbIssues.SelectionChanged += self.on_pick
        self.btnZoom.Click += lambda s, a: self.act("zoom")
        self.btn3D.Click += lambda s, a: self.act("3d")
        self.btnIsolate.Click += lambda s, a: self.act("isolate")
        self.btnSelect.Click += lambda s, a: self.act("select")
        self.btnProps.Click += self.on_copy_ids
        self.btnResolve.Click += lambda s, a: self.set_review(bcf.RESOLVED)
        self.btnIgnore.Click += lambda s, a: self.set_review(bcf.IGNORED)
        self.btnReopen.Click += lambda s, a: self.set_review(bcf.OPEN)
        self.btnPrev.Click += lambda s, a: self.step(-1)
        self.btnNext.Click += lambda s, a: self.step(1)
        self.btnColour.Click += self.on_colour
        self.btnClear.Click += self.on_clear
        self.bdSnapshot.MouseLeftButtonUp += self.on_snapshot
        self.tbNote.LostFocus += self.on_note
        self.tbNote.PreviewKeyDown += self.on_note_key
        self.btnSend.Click += lambda s, a: self.send_comment()
        self.btnAssign.Click += self.on_assign
        self.btnMenu.Click += self.on_menu
        self.btnUndoFix.Click += lambda s, a: self.undo_fix()
        self.btnCommand.Click += lambda s, a: self.open_command()
        for lb in (self.lbIssues, self.lbProjects, self.lbCommand):
            self.hoverable_list(lb)
        self.btnStartBrowse.Click += lambda s, a: self.on_browse(None, None)
        self.btnStartSignIn.Click += lambda s, a: self.sign_in()
        self.pnlNoFilesAction.Click += lambda s, a: self._empty_action and self._empty_action()
        self._empty_action = None
        self.bdCommandDim.MouseLeftButtonUp += lambda s, a: self.close_command()
        self.tbCommand.TextChanged += lambda s, a: self.fill_command()
        self.tbCommand.PreviewKeyDown += self.on_command_key
        self.lbCommand.MouseLeftButtonUp += lambda s, a: self.run_command()
        self.iconise()
        self.btnSelection.MouseLeftButtonUp += lambda s, a: self.toggle_selection()
        self.btnUpdateNow.Click += lambda s, a: self.update_now()
        self.btnUpdateLater.Click += lambda s, a: self.update_later()
        self.btnTourNext.Click += lambda s, a: self.show_tour_step(self.tour_step + 1)
        self.btnTourBack.Click += lambda s, a: self.show_tour_step(self.tour_step - 1)
        self.btnTourSkip.Click += lambda s, a: self.end_tour()
        self.rootGrid.SizeChanged += lambda s, a: self.tour_step is not None and self.show_tour_step(self.tour_step)
        self.PreviewKeyDown += self.on_key
        for key, name, what in SHORTCUTS:
            b = getattr(self, name)
            tip = b.ToolTip if isinstance(b.ToolTip, basestring) and b.ToolTip else what
            b.ToolTip = u"{} ({})".format(tip, key)
        self._theme_handler = None
        try:  # Revit 2024 and later: follow Revit's theme as soon as it changes
            self._theme_handler = lambda s, a: self.on_revit_theme()
            HOST_APP.uiapp.ThemeChanged += self._theme_handler
        except Exception:
            self._theme_handler = None
        self.update_account()

    def start(self):
        """Opens the last results (the same account's only), goes live with BIMPulse, tidies the
        download cache, checks for a new version and, the first time, shows the tour."""
        if self.started:
            return
        self.started = True
        self.timer.Start()
        self.requests.Start()
        last = [p for p in self.settings.get("files", []) if os.path.exists(p)]
        if last and (self.settings.get("account") or None) != self._account():
            # Opened by another account (or signed out since): never show its results to this one.
            last = []
            self.forget_files()
        if last:
            self.load(last, self.settings.get("cloud") or {})
        else:
            self.refresh()
            self.status("Click Browse BIMPulse to open a saved audit's issues.")
        keep = list(self.settings.get("files", []))
        threading.Thread(target=lambda: cloud.clean_cache(keep)).start()
        self.check_updates()
        if not self.settings.get("tour_done"):
            self.later(0.8, self.start_tour)

    def later(self, seconds, action):
        """Runs action() on the UI thread after a moment (once the layout has settled)."""
        timer = DispatcherTimer()
        timer.Interval = System.TimeSpan.FromSeconds(seconds)

        def tick(s, a):
            timer.Stop()
            action()
        timer.Tick += tick
        timer.Start()

    # ---------- helpers ----------

    def status(self, message, error=False, good=False):
        self.txtStatus.Text = message
        self.txtStatus.Foreground = brush("#EF4444" if error else ("#22C55E" if good else "#8A9BB3"))

    def announce(self, title, message):
        """A green status line plus a Windows notification, for things done in the model."""
        self.status(u"\u2713 " + message, good=True)
        try:
            forms.toast(message, title=title, appid="BIMPulse Dashboard")
        except Exception:
            pass

    def background(self, work, done=None):
        """Runs work() off the UI thread (BIMPulse calls), then done(result) on it."""
        def run():
            try:
                result, failed = work(), None
            except Exception as e:
                result, failed = None, e

            def finish():
                if failed is not None:
                    self.cloud_error(failed)
                elif done is not None:
                    try:
                        done(result)
                    except Exception as e:
                        self.status("Something went wrong: {}".format(e), error=True)
            self.Dispatcher.Invoke(System.Action(finish))
        t = threading.Thread(target=run)
        t.daemon = True
        t.start()

    def cloud_error(self, e):
        self.list_loading(False)
        if isinstance(e, cloud.SignedOut):
            self.session = None
            self.update_account()
        message = str(e) if isinstance(e, cloud.CloudError) else "BIMPulse: {}".format(e)
        self.status(message, error=True)
        self.txtBrowseNote.Text = message

    def run(self, job):
        """Queues job(uidoc, finder) for Revit's API context."""
        def wrapped(uiapp):
            uidoc = uiapp.ActiveUIDocument
            if uidoc is None:
                self.status("Open a model in Revit first.", error=True)
                return
            job(uidoc, self.finder(uidoc.Document))
        self.queue.jobs.append(wrapped)
        self.event.Raise()

    def finder(self, doc):
        key = doc.PathName or doc.Title
        if key not in self.finders:
            self.finders[key] = rv.Finder(doc)
        return self.finders[key]

    @property
    def topics(self):
        return [t for f in self.files for t in f.topics]

    # ---------- loading ----------

    def _tags(self):
        """{path: online source} of the open files that came from BIMPulse."""
        return dict((f.path, f.cloud) for f in self.files if f.cloud)

    def load(self, paths, tags=None):
        """Opens BCF files. `tags`: {path: {"audit_id", "module", "consultant", "can_review"}} for
        files downloaded from BIMPulse, whose statuses are shared online instead of saved beside them."""
        tags = dict((os.path.normcase(os.path.abspath(k)), v) for k, v in (tags or {}).items())
        files, errors, seen = [], [], set()
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            try:
                f = bcf.load(p)
            except Exception as e:
                errors.append("{} ({})".format(os.path.basename(p), e))
                continue
            f.cloud = tags.get(key)
            if f.cloud:
                for t in f.topics:  # the shared status replaces anything in the file
                    t.review, t.note, t.reviewed_by, t.reviewed_at = bcf.OPEN, "", "", ""
            files.append(f)
        self.files = files
        self.settings["files"] = [f.path for f in files]
        self.settings["cloud"] = self._tags()
        # Whose results these are: they reopen next time only for the same account.
        self.settings["account"] = self._account()
        local = [f for f in files if not f.cloud]
        if local:
            self.settings["folder"] = os.path.dirname(local[0].path)
        save_settings(self.settings)
        for finder in self.finders.values():
            finder.cache.clear()
        names = sorted(set(t.consultant for t in self.topics if t.consultant))
        self.cbConsultant.SelectionChanged -= self.on_filter
        self.cbConsultant.Items.Clear()
        self.cbConsultant.Items.Add("All consultants")
        for n in names:
            self.cbConsultant.Items.Add(n)
        self.cbConsultant.SelectedIndex = 0
        self.cbConsultant.SelectionChanged += self.on_filter
        self.audit_filter = None
        self.statFound.Text = u"–"
        self.statFoundSub.Text = u"Run “Check against model”"
        self.refresh(animate=True)
        msg = "Loaded {} issue{} from {} file{}.".format(len(self.topics), "" if len(self.topics) == 1 else "s",
                                                        len(files), "" if len(files) == 1 else "s")
        if errors:
            self.status(msg + " Could not read: " + "; ".join(errors), error=True)
        else:
            self.status(msg)
        self.sync_reviews()

    def sync_reviews(self):
        """Fetches the shared statuses of the files that came from BIMPulse."""
        audits = sorted(set(f.cloud["audit_id"] for f in self.files if f.cloud))
        if not audits:
            return
        if self.session is None:
            self.status("Sign in to BIMPulse to see and share the issue statuses of these audits.", error=True)
            return
        session = self.session

        def work():
            people = session.people() if not self.people else self.people
            return people, dict((a, (session.reviews(a), session.events(a, limit=500))) for a in audits)

        def done(result):
            self.people, by_audit = result
            for a, (_, events) in by_audit.items():
                self.since[a] = cloud.newest(events, self.since.get(a)) or "1970-01-01T00:00:00+00:00"
                self.seen.note_events(events, session.email, a)
            for f in self.files:
                if f.cloud and f.cloud["audit_id"] in by_audit:
                    cloud.apply_reviews(f, by_audit[f.cloud["audit_id"]][0])
            self.refresh()
            self.status("Up to date with BIMPulse. New comments and statuses arrive every {} seconds.".format(
                LIVE_SECONDS))
            self.load_changes()
        self.background(work, done)

    # ---------- dashboard ----------

    def refresh(self, keep=None, animate=False):
        topics = self.topics
        project = next((f.manifest.get("project") or f.project for f in self.files
                        if f.manifest.get("project") or f.project), "")
        revision = next((f.manifest.get("revision") for f in self.files if f.manifest.get("revision")), "")
        self.txtProject.Text = " · ".join(x for x in (project, revision and "Rev " + revision) if x) \
            or "BIMPulse Dashboard"
        total = len(topics)
        open_ = [t for t in topics if t.review == bcf.OPEN]
        done = sum(1 for t in topics if t.review != bcf.OPEN)
        self.statTotal.Text = "{:,}".format(total)
        self.statFiles.Text = "{} result{} open".format(len(self.files), "" if len(self.files) == 1 else "s") \
            if self.files else "No results open"
        self.statFiles.ToolTip = "\n".join(f.name for f in self.files) or None
        self.statOpen.Text = "{:,}".format(len(open_))
        high = sum(1 for t in open_ if t.severity in ("critical", "error"))
        self.statCritical.Text = "{:,} critical or error".format(high)
        pct = int(round(100.0 * done / total)) if total else 0
        self.statResolved.Text = "{}%".format(pct)
        self.colResolved.Width = GridLength(pct, GridUnitType.Star)
        self.colUnresolved.Width = GridLength(100 - pct, GridUnitType.Star)
        self.draw_ring(pct if total else 0)
        score = plug.average(f.cloud.get("score") for f in self.files if f.cloud)
        self.txtScore.Text = u"Score {}%".format(score) if score is not None else ""
        self.txtScore.ToolTip = (u"Audit score {}% ({}): the saved audit's score for the open results. The ring is "
                                 u"live: it fills as issues are resolved.".format(score, plug.grade(score))
                                 if score is not None else None)
        self.txtScore.Foreground = brush({"Green": "#22C55E", "Amber": "#F59E0B", "Red": "#EF4444"}.get(
            plug.grade(score), "#8A9BB3"))
        if total and pct == 100 and self._last_pct is not None and self._last_pct < 100:
            self.celebrate()
        self._last_pct = pct if total else None
        self._breakdown(topics)
        self._fill_list(keep, animate)
        self.show_start()

    def _breakdown(self, topics):
        counts = {}
        for t in topics:
            if t.review == bcf.OPEN:
                counts[t.severity] = counts.get(t.severity, 0) + 1
        self.gridSeverity.Children.Clear()
        self.gridSeverity.ColumnDefinitions.Clear()
        self.spSeverityKey.Children.Clear()
        col = 0
        for sev in ("critical", "error", "warning", "info"):
            n = counts.get(sev, 0)
            if not n:
                continue
            cd = ColumnDefinition()
            cd.Width = GridLength(n, GridUnitType.Star)
            self.gridSeverity.ColumnDefinitions.Add(cd)
            seg = Border()
            seg.Background = brush(SEV_COLOUR[sev])
            seg.Margin = Thickness(0, 0, 2, 0)
            seg.CornerRadius = CornerRadius(3)
            seg.ToolTip = "{} open {}".format(n, sev)
            Grid.SetColumn(seg, col)
            self.gridSeverity.Children.Add(seg)
            self.spSeverityKey.Children.Add(text(u"● {} {}".format(n, sev), 10.5, SEV_COLOUR[sev],
                                                 mono=True, margin=(10, 0, 0, 0)))
            col += 1
        self.wpAudits.Children.Clear()
        by_audit = {}
        for t in topics:
            by_audit.setdefault(t.audit, [t.audit_name, 0, 0])
            by_audit[t.audit][1] += 1
            if t.review == bcf.OPEN:
                by_audit[t.audit][2] += 1
        for audit in sorted(by_audit, key=lambda a: -by_audit[a][1]):
            name, n, n_open = by_audit[audit]
            chip = Border()
            chip.CornerRadius = CornerRadius(14)
            chip.Padding = Thickness(11, 4, 11, 4)
            chip.Margin = Thickness(0, 0, 6, 6)
            chip.Cursor = Cursors.Hand
            active = self.audit_filter == audit
            chip.Background = brush("#14304D" if active else "#10203A")
            chip.BorderBrush = brush("#00A0E3" if active else "#1C2B40")
            chip.BorderThickness = Thickness(1)
            sp = StackPanel()
            sp.Orientation = Orientation.Horizontal
            sp.Children.Add(text(name, 12, "#E6EEF7", bold=True))
            sp.Children.Add(text("{}/{}".format(n_open, n), 11, "#00A0E3", mono=True, margin=(8, 1, 0, 0)))
            chip.Child = sp
            sev = {}
            for x in topics:
                if x.audit == audit and x.review == bcf.OPEN:
                    sev[x.severity] = sev.get(x.severity, 0) + 1
            chip.ToolTip = hover_card(name, [("Open", u"{} of {}".format(n_open, n)), ("Resolved", n - n_open)] +
                                      [(k.title(), sev[k]) for k in ("critical", "error", "warning", "info") if sev.get(k)],
                                      "Click to show only these issues (click again for all)")
            chip.MouseLeftButtonUp += (lambda a: lambda s, e: self.toggle_audit(a))(audit)
            self.hoverable(chip)
            self.wpAudits.Children.Add(chip)

    def toggle_audit(self, audit):
        self.audit_filter = None if self.audit_filter == audit else audit
        self.refresh()

    def filtered(self):
        query = (self.tbSearch.Text or "").strip().lower()
        consultant = self.cbConsultant.SelectedItem if self.cbConsultant.SelectedIndex > 0 else None
        severity = (self.cbSeverity.SelectedItem or "").lower() if self.cbSeverity.SelectedIndex > 0 else None
        review = {0: bcf.OPEN, 2: bcf.RESOLVED, 3: bcf.IGNORED}.get(self.cbReview.SelectedIndex)
        me = self.my_id()
        today = datetime.date.today().isoformat()
        out = []
        for t in self.topics:
            if self.audit_filter and t.audit != self.audit_filter:
                continue
            if consultant and t.consultant != consultant:
                continue
            if severity and t.severity != severity:
                continue
            if review and t.review != review:
                continue
            if self.cbReview.SelectedIndex == 4 and (not me or t.assignee != me):
                continue
            if self.cbReview.SelectedIndex == 5 and not cloud.is_overdue(t, today):
                continue
            if query and not all(w in t.search_text() for w in query.split()):
                continue
            if self.selection is not None and not plug.in_selection(t, *self.selection):
                continue
            if self.win_filter is not None and t.key not in self.win_filter[0]:
                continue
            out.append(t)
        sort = self.cbSort.SelectedIndex
        if sort == 1:
            out.sort(key=lambda t: (t.audit_name, t.severity_rank, t.index))
        elif sort == 2:
            out.sort(key=lambda t: t.title.lower())
        elif sort == 3:
            out.sort(key=lambda t: (-len(t.elements), t.severity_rank))
        else:
            out.sort(key=lambda t: (t.severity_rank, t.audit_name, t.index))
        return out

    def _fill_list(self, keep=None, animate=False):
        keep = keep or self.current
        shown = self.filtered()
        self.lbIssues.SelectionChanged -= self.on_pick
        self.lbIssues.Items.Clear()
        pick = None
        for n, t in enumerate(shown[:LIST_LIMIT]):
            item = ListBoxItem()
            item.Tag = t
            item.Content = self._row(t)
            self.lbIssues.Items.Add(item)
            if animate and n < 14:
                animate_in(item, 6, 200, n * 22)
            if keep is not None and t is keep:
                pick = item
        if pick is not None:  # keep the selection without zooming to it again
            self.lbIssues.SelectedItem = pick
            self.lbIssues.ScrollIntoView(pick)
        self.lbIssues.SelectionChanged += self.on_pick
        more = " (showing the first {:,})".format(LIST_LIMIT) if len(shown) > LIST_LIMIT else ""
        self.txtCount.Text = "{:,} ISSUE{}{}{}".format(len(shown), "" if len(shown) == 1 else "S", more.upper(),
                                                        u" \u00b7 " + self.win_filter[1].upper() if self.win_filter else "")
        self._empty_state(shown)
        self.show_detail(pick.Tag if pick is not None else None)

    def _row(self, t):
        g = Grid()
        c0, c1 = ColumnDefinition(), ColumnDefinition()
        c0.Width = GridLength(4)
        g.ColumnDefinitions.Add(c0)
        g.ColumnDefinitions.Add(c1)
        strip = Border()
        strip.Background = brush(SEV_COLOUR.get(t.severity, "#F59E0B"))
        strip.CornerRadius = CornerRadius(10, 0, 0, 10)
        g.Children.Add(strip)
        body = StackPanel()
        body.Margin = Thickness(12, 9, 12, 9)
        Grid.SetColumn(body, 1)
        title = text(t.title, 13, "#E6EEF7" if t.review == bcf.OPEN else "#8A9BB3", bold=True, wrap=True)
        title.MaxHeight = 36
        title.TextTrimming = TextTrimming.CharacterEllipsis
        body.Children.Add(title)
        meta = Grid()
        m0, m1 = ColumnDefinition(), ColumnDefinition()
        m1.Width = GridLength.Auto
        meta.ColumnDefinitions.Add(m0)
        meta.ColumnDefinitions.Add(m1)
        meta.Margin = Thickness(0, 5, 0, 0)
        n = len(t.elements)
        bits = [t.audit_name, t.consultant, "{} element{}".format(n, "" if n == 1 else "s") if n else "Model-wide"]
        meta.Children.Add(text(u"  ·  ".join(b for b in bits if b), 11.5, "#8A9BB3"))
        right = StackPanel()
        right.Orientation = Orientation.Horizontal
        Grid.SetColumn(right, 1)
        res = self._cached(t)
        if res is not None and not res.model_wide:
            ok = res.found
            right.Children.Add(text(u"●", 10, "#22C55E" if ok == len(res.rows) else
                                    ("#F59E0B" if ok else "#EF4444"), margin=(0, 1, 8, 0)))
        if self.seen.unread(t):
            news = pill("NEW", "#00A0E3")
            news.Margin = Thickness(0, 0, 6, 0)
            news.ToolTip = "New comments or changes since you last opened it"
            right.Children.Add(news)
        if t.assignee_name:
            late = cloud.is_overdue(t, datetime.date.today().isoformat())
            who = pill(u"{}{}".format(initials(t.assignee_name), u" · " + short_date(t.due_on) if t.due_on else ""),
                       "#EF4444" if late else "#8A9BB3")
            who.Margin = Thickness(0, 0, 6, 0)
            person = next((x for x in self.people if x.get("id") == t.assignee), {})
            theirs = [x for x in self.topics if x.assignee == t.assignee and x.review == bcf.OPEN]
            who.ToolTip = hover_card(t.assignee_name, [
                ("Email", person.get("email")),
                ("Due", (u"Overdue \u00b7 " if late else u"") + short_date(t.due_on) if t.due_on else "No due date"),
                ("Open issues", u"{} assigned to them here".format(len(theirs))),
                ("Overdue", sum(1 for x in theirs if cloud.is_overdue(x, datetime.date.today().isoformat())) or None)])
            right.Children.Add(who)
        if t.review != bcf.OPEN:
            right.Children.Add(pill(t.review.upper(), REVIEW_COLOUR[t.review]))
        meta.Children.Add(right)
        body.Children.Add(meta)
        g.Children.Add(body)
        return g

    def _cached(self, t):
        for f in self.finders.values():
            if t.key in f.cache:
                return f.cache[t.key]
        return None

    # ---------- detail ----------

    def on_pick(self, sender, args):
        item = self.lbIssues.SelectedItem
        t = item.Tag if item is not None else None
        self.show_detail(t)
        # Picking an issue selects its elements without moving the view; Select & zoom (or
        # "Zoom as I browse") zooms to them.
        if t is not None and self.chkAuto.IsChecked and t.elements:
            self.act("zoom", quiet=True)
        elif t is not None and t.elements:
            self.act("select", quiet=True)

    def show_detail(self, t):
        same = t is not None and t is self.current
        self.current = t
        self.pnlEmpty.Visibility = Visibility.Visible if t is None else Visibility.Collapsed
        self.pnlDetail.Visibility = Visibility.Collapsed if t is None else Visibility.Visible
        self.pnlWins.Visibility = Visibility.Collapsed
        if t is None:
            self.show_wins()
            return
        if not same:
            animate_in(self.pnlDetail, 6, 170)
        colour = SEV_COLOUR.get(t.severity, "#F59E0B")
        self.bdSeverity.Background = brush("#26" + colour[1:])
        self.txtSeverity.Text = SEV_LABEL.get(t.severity, t.severity.upper())
        self.txtSeverity.Foreground = brush(colour)
        rc = REVIEW_COLOUR[t.review]
        self.bdReview.Background = brush("#26" + rc[1:])
        self.txtReview.Text = t.review.upper()
        self.txtReview.Foreground = brush(rc)
        self.txtMeta.Text = u"  ·  ".join(x for x in (t.audit_name.upper(), (t.consultant or "").upper(),
                                                           "#{}".format(t.index) if t.index else "") if x)
        self.txtTitle.Text = t.title
        self.txtSub.Text = u"  ·  ".join(x for x in (t.group, t.category if t.category != t.group else "",
                                                          t.source.name if t.source else "") if x)
        reasons = " ".join(f.get("reason", "") for f in t.failures)
        desc = (t.description or "").strip()
        self.txtDescription.Text = desc if desc and desc not in reasons else ""
        self.txtDescription.Visibility = Visibility.Visible if self.txtDescription.Text else Visibility.Collapsed
        self.spFailures.Children.Clear()
        for fl in t.failures or [{"field": "", "reason": t.description or "No details in the BCF."}]:
            row = Border()
            row.Background = brush("#081221")
            row.CornerRadius = CornerRadius(8)
            row.Padding = Thickness(10, 7, 10, 7)
            row.Margin = Thickness(0, 0, 0, 5)
            sp = StackPanel()
            if fl.get("field"):
                sp.Children.Add(text(fl["field"].upper(), 10.5, "#00A0E3", bold=True, mono=True))
            sp.Children.Add(text(fl.get("reason", ""), 12.5, "#E6EEF7", wrap=True))
            row.Child = sp
            self.spFailures.Children.Add(row)
        self._fix_panel(t)
        self._snapshot(t)
        self._elements(t, self._cached(t))
        self.pnlComments.Visibility = Visibility.Visible if t.comments else Visibility.Collapsed
        self.spComments.Children.Clear()
        for c in t.comments:
            sp = StackPanel()
            sp.Margin = Thickness(0, 0, 0, 6)
            sp.Children.Add(text(u"{}  ·  {}".format(c.get("author") or "", (c.get("date") or "")[:10]),
                                 10.5, "#8A9BB3", mono=True))
            sp.Children.Add(text(c.get("text"), 12.5, wrap=True))
            self.spComments.Children.Add(sp)
        self.tbNote.Text = t.note or ""
        self.txtReviewedBy.Text = u"{} by {} · {}".format(t.review.title(), t.reviewed_by, t.reviewed_at) \
            if t.reviewed_by and t.review != bcf.OPEN else ""
        has = bool(t.elements)
        for b in (self.btnZoom, self.btn3D, self.btnIsolate, self.btnSelect, self.btnProps):
            b.IsEnabled = has
        can = self._can_review(t)
        self.btnResolve.IsEnabled = can and t.review != bcf.RESOLVED
        self.btnIgnore.IsEnabled = can and t.review != bcf.IGNORED
        self.btnReopen.IsEnabled = can and t.review != bcf.OPEN
        self.tbNote.IsReadOnly = not can
        self.tbNote.ToolTip = None if can else VIEWER_NOTE
        online = bool(t.source and t.source.cloud)
        self.pnlThread.Visibility = Visibility.Visible if online else Visibility.Collapsed
        self.btnSend.Visibility = Visibility.Visible if online and can else Visibility.Collapsed
        self.chkSnapshot.Visibility = Visibility.Visible if online and can else Visibility.Collapsed
        self._assign_panel(t, online, can)
        picked = self.lbIssues.SelectedItems.Count
        if picked > 1:
            self.status("{} issues selected: Mark resolved, Ignore and Reopen change them all (with the comment "
                        "typed, which ignoring needs).".format(picked))
        self.lblNote.Text = "ADD A COMMENT" if online else "YOUR NOTE"
        if online:
            if not same:  # keep a half-written comment when the list refreshes
                self.tbNote.Text = ""
                self.spThread.Children.Clear()
                self.spThread.Children.Add(skeleton(2, 46))
                self.txtThreadState.Text = ""
                self.chkSnapshot.IsChecked = False
                self.load_thread(t)
                if self.seen.unread(t):
                    self.seen.mark(t)

    def load_thread(self, t):
        if self.session is None:
            self.txtThreadState.Text = "Sign in to see it"
            return
        c, session = t.source.cloud, self.session

        def done(events):
            if self.current is t:
                self.render_thread(events)
        self.background(lambda: session.events(c["audit_id"], module=c["module"], consultant=c["consultant"],
                                               issue_key=t.issue_key), done)

    def render_thread(self, events):
        self.spThread.Children.Clear()
        self.txtThreadState.Text = "{} update{}".format(len(events), "" if len(events) == 1 else "s") if events else ""
        if not events:
            self.spThread.Children.Add(text("No comments yet. What you send shows on the BIMPulse Issues page "
                                            "straight away.", 12, "#8A9BB3", wrap=True))
            return
        for e in reversed(events):  # oldest first
            box = Border()
            box.CornerRadius = CornerRadius(8)
            box.Padding = Thickness(10, 7, 10, 7)
            box.Margin = Thickness(0, 0, 0, 5)
            status = e.get("kind") in ("status", "assign")
            box.Background = brush("#00000000" if status else "#081221")
            box.BorderBrush = brush("#1C2B40")
            box.BorderThickness = Thickness(1)
            sp = StackPanel()
            head = StackPanel()
            head.Orientation = Orientation.Horizontal
            head.Children.Add(text(e.get("author_name") or "Someone", 11.5, "#E6EEF7", bold=True))
            revit = e.get("source") == "revit"
            badge = pill("REVIT" if revit else "WEB", "#00A0E3" if revit else "#8A9BB3")
            badge.Margin = Thickness(8, 0, 8, 0)
            head.Children.Add(badge)
            head.Children.Add(text(cloud._when(e.get("created_at")), 11, "#5D6F88"))
            sp.Children.Add(head)
            if e.get("kind") == "assign":
                sp.Children.Add(text(e.get("body") or "Assignment changed", 12.5, "#00A0E3", wrap=True, margin=(0, 3, 0, 0)))
            elif status:
                label = {"resolved": "Marked resolved", "ignored": "Ignored"}.get(e.get("status"), "Reopened")
                sp.Children.Add(text(label, 12.5, "#00A0E3", margin=(0, 3, 0, 0)))
                if e.get("body"):
                    sp.Children.Add(text(e["body"], 12, "#8A9BB3", wrap=True, margin=(0, 2, 0, 0)))
            elif e.get("body"):
                sp.Children.Add(text(e.get("body") or "", 12.5, wrap=True, margin=(0, 3, 0, 0)))
            if e.get("attachment"):
                sp.Children.Add(self._attachment(e["attachment"]))
            if e.get("viewpoint"):
                sp.Children.Add(self._viewpoint_link(e["viewpoint"]))
            box.Child = sp
            self.spThread.Children.Add(box)

    def _viewpoint_link(self, vp):
        """'Go to this view' under a comment sent with a Revit view."""
        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        row.Margin = Thickness(0, 6, 0, 0)
        go = Button()
        go.Content = u"Go to this view"
        go.Padding = Thickness(10, 3, 10, 3)
        go.ToolTip = u"Opens the view it was sent from{}".format(
            u" (in {})".format(vp.get("document")) if vp.get("document") else "")
        go.Click += lambda s, a: self.goto_viewpoint(vp)
        row.Children.Add(go)
        what = vp.get("view") or ("3D view" if vp.get("eye") else "view")
        row.Children.Add(text(what, 11.5, "#8A9BB3", margin=(8, 4, 0, 0)))
        return row

    def goto_viewpoint(self, vp):
        def job(uidoc, finder):
            try:
                name = rv.restore_viewpoint(uidoc, vp)
            except Exception as e:
                self.status(u"Could not open the view: {}".format(e), error=True)
                return
            other = vp.get("document") and vp["document"] != uidoc.Document.Title
            self.status(u"\u2713 Opened the view '{}'.{}".format(
                name, u" It was sent from {}, so check you are in the right model.".format(vp["document"])
                if other else ""), good=True)
        self.run(job)

    def on_note_key(self, sender, args):
        t = self.current
        if t is not None and t.source and t.source.cloud and args.Key == Key.Enter \
                and Keyboard.Modifiers == ModifierKeys.Control:
            args.Handled = True
            self.send_comment()

    def send_comment(self):
        t = self.current
        body = (self.tbNote.Text or "").strip()
        snap = bool(self.chkSnapshot.IsChecked)
        if t is None or not (body or snap) or not (t.source and t.source.cloud):
            return
        if not self._can_review(t):
            self.status(VIEWER_NOTE, error=True)
            return
        self._post(t, cloud.event_payload(t, "comment", body=body), "Sent to BIMPulse.", snapshot=snap)
        self.tbNote.Text = ""

    def _post(self, t, payload, done_message, snapshot=False, good=False):
        """Sends a comment or status change to BIMPulse (with a picture of the active view when
        `snapshot`), then shows it in the conversation."""
        if self.session is None:
            self.status("Sign in to BIMPulse to share this with your team.", error=True)
            return
        if not payload:
            self.status("This issue has no BIMPulse key (an older export), so it can't be shared.", error=True)
            return
        audit_id, session = t.source.cloud["audit_id"], self.session

        def done(events):
            self.since[audit_id] = cloud.newest(events, self.since.get(audit_id))
            if good:
                self.announce("BIMPulse: fixed", done_message)
            else:
                self.status(done_message)
            if self.current is t:
                self.load_thread(t)

        def send(png=None, viewpoint=None):
            body = dict(payload)
            if viewpoint:
                body["viewpoint"] = viewpoint
            if (png or viewpoint) and body["kind"] == "status" and not body.get("body"):
                body["body"] = "View attached."
            if body["kind"] == "comment" and not body.get("body") and not png and not viewpoint:
                self.status("Nothing to send: type a comment, or attach the view.")
                return
            self.send_or_keep("event", audit_id, body, done, png=png)

        def draw_then_send(png, viewpoint):
            if png is None:
                send(None, viewpoint)
                return
            try:
                drawn = markup.markup(png, self.Resources, brush, SANS_FONT)
            except Exception:
                debug(u"markup failed: {}".format(traceback.format_exc()))
                drawn = png
            if drawn is None:  # sent without the view
                send(None, None)
            else:
                send(drawn, viewpoint)

        if not snapshot:
            send()
            return
        self.chkSnapshot.IsChecked = False
        self.status("Capturing the active view...")

        def job(uidoc, finder):
            try:
                viewpoint = rv.capture_viewpoint(uidoc)
            except Exception:
                debug(u"could not read the viewpoint: {}".format(traceback.format_exc()))
                viewpoint = None
            try:
                png = rv.snapshot_png(uidoc)
            except Exception as e:
                self.status("Could not capture a picture of the view: {}. Sent without it.".format(e), error=True)
                png = None
            # Draw on it once Revit's API call has finished (the window waits for the person).
            self.Dispatcher.BeginInvoke(System.Action(lambda: draw_then_send(png, viewpoint)))
        self.run(job)

    def send_or_keep(self, kind, audit_id, body, done, png=None, queued=None):
        """Sends a change to BIMPulse; when it can't be reached, keeps it on this PC (the outbox)
        and sends it on its own once it can. `queued()` updates the dashboard in that case."""
        session = self.session
        kept = []

        def work():
            try:
                b = dict(body)
                if png:
                    b["attachment"] = session.attach(audit_id, png)
                if kind == "event":
                    return session.add_event(audit_id, b)
                if kind == "bulk":
                    return session.bulk(audit_id, b)
                return session.assign(audit_id, b)
            except Exception as e:
                if not cloud.offline(e):
                    raise
                kept.append(self.outbox.add(session.email, kind, audit_id, body, png))
                return None

        def finish(result):
            if kept:
                if queued is not None:
                    queued()
                self.status(u"BIMPulse can't be reached: saved on this PC, and sent by itself when you're back "
                            u"online ({} waiting).".format(kept[0]), error=True)
                return
            done(result)
        self.background(work, finish)

    def flush_outbox(self):
        """Sends what was kept while offline (every poll, and after signing in)."""
        session = self.session
        if self.flushing or session is None or not self.outbox.count(session.email):
            return
        self.flushing = True

        def work():
            try:
                return self.outbox.flush(session)
            finally:
                self.flushing = False

        def done(result):
            sent, refused = result
            if refused:
                self.status(u"BIMPulse turned down {} saved change{}: {}".format(
                    len(refused), "" if len(refused) == 1 else "s", refused[0]), error=True)
            elif sent:
                self.status(u"\u2713 Back online: sent {} saved change{} to BIMPulse.".format(
                    sent, "" if sent == 1 else "s"), good=True)
            if refused:
                self.sync_reviews()  # back to BIMPulse's statuses where it said no
        self.quietly(work, done)

    def _attachment(self, path):
        """An image sent with a comment: loaded in the background, click to open full size."""
        holder = Border()
        holder.Margin = Thickness(0, 6, 0, 0)
        holder.CornerRadius = CornerRadius(6)
        holder.BorderBrush = brush("#1C2B40")
        holder.BorderThickness = Thickness(1)
        holder.Child = text("Loading image...", 11.5, "#5D6F88", margin=(8, 6, 8, 6))

        def show(data):
            self.images[path] = data
            try:
                img = BitmapImage()
                img.BeginInit()
                img.StreamSource = MemoryStream(System.Array[System.Byte](bytearray(data)))
                img.CacheOption = BitmapCacheOption.OnLoad
                img.DecodePixelWidth = 560
                img.EndInit()
                img.Freeze()
                view = Image()
                view.Source = img
                view.MaxHeight = 240
                holder.Child = view
                holder.Cursor = Cursors.Hand
                holder.ToolTip = "Click to open full size"
                holder.MouseLeftButtonUp += lambda s, a: self._open_image(path)
            except Exception:
                holder.Child = text("The image could not be shown.", 11.5, "#8A9BB3", margin=(8, 6, 8, 6))

        if path in self.images:
            show(self.images[path])
        elif self.session is not None:
            session = self.session
            self.background(lambda: session.attachment(path), show)
        return holder

    def _open_image(self, path):
        data = self.images.get(path)
        if not data:
            return
        target = os.path.join(tempfile.gettempdir(), "bimpulse-" + os.path.basename(path))
        with open(target, "wb") as f:
            f.write(data)
        os.startfile(target)

    def my_id(self):
        email = (self.session.email or "").lower() if self.session else ""
        return next((p["id"] for p in self.people if (p.get("email") or "").lower() == email and email), None)

    def _assign_panel(self, t, online, can):
        # Consultants work on their issues but don't assign them.
        online = online and bool(t.source.cloud.get("can_assign", True))
        self.pnlAssign.Visibility = Visibility.Visible if online else Visibility.Collapsed
        if not online:
            return
        self.cbAssignee.Items.Clear()
        self.cbAssignee.Items.Add("Unassigned")
        self._assignee_ids = [None]
        for p in self.people:
            self.cbAssignee.Items.Add(p.get("name") or p.get("email") or "Someone")
            self._assignee_ids.append(p["id"])
        if t.assignee and t.assignee not in self._assignee_ids:
            self.cbAssignee.Items.Add(t.assignee_name or "Someone")
            self._assignee_ids.append(t.assignee)
        self.cbAssignee.SelectedIndex = self._assignee_ids.index(t.assignee) if t.assignee in self._assignee_ids else 0
        if t.due_on:
            y, m, d = [int(x) for x in t.due_on.split("-")]
            self.dpDue.SelectedDate = System.DateTime(y, m, d)
        else:
            self.dpDue.SelectedDate = None
        for c in (self.cbAssignee, self.dpDue, self.btnAssign):
            c.IsEnabled = can
        late = cloud.is_overdue(t, datetime.date.today().isoformat())
        self.txtAssign.Text = "Overdue." if late else ("" if self.people else "Sign in to assign issues.")
        self.txtAssign.Foreground = brush("#EF4444" if late else "#8A9BB3")

    def on_assign(self, sender, args):
        t = self.current
        if t is None or self.session is None or not (t.source and t.source.cloud):
            return
        if not self._can_review(t):
            self.status(VIEWER_NOTE, error=True)
            return
        n = self.cbAssignee.SelectedIndex
        assignee = self._assignee_ids[n] if 0 <= n < len(self._assignee_ids) else None
        when = self.dpDue.SelectedDate
        due = "{:04d}-{:02d}-{:02d}".format(when.Year, when.Month, when.Day) if when is not None else None
        payload = cloud.assign_payload(t, assignee, due)
        if payload is None:
            self.status("This issue has no BIMPulse key (an older export), so it can't be assigned.", error=True)
            return
        audit_id, session = t.source.cloud["audit_id"], self.session

        def done(event):
            t.assignee, t.due_on = assignee, due or ""
            t.assignee_name = self.cbAssignee.SelectedItem if assignee else ""
            self.since[audit_id] = cloud.newest([event], self.since.get(audit_id))
            self.status(event.get("body") or "Saved.")
            self.refresh(keep=t)
            if self.current is t:
                self.load_thread(t)
        def queued():
            t.assignee, t.due_on = assignee, due or ""
            t.assignee_name = self.cbAssignee.SelectedItem if assignee else ""
            self.refresh(keep=t)
        self.send_or_keep("assign", audit_id, payload, done, queued=queued)

    # ---------- fixing the model ----------

    def _fixable(self, t):
        if not t.elements:
            return []
        return [f for f in t.failures if "value" in f and f.get("field") and f["field"] not in NOT_FIXABLE]

    def _fix_panel(self, t):
        self.spFix.Children.Clear()
        fixable = self._fixable(t)
        self.pnlFix.Visibility = Visibility.Visible if fixable else Visibility.Collapsed
        done = self.fixed.get(t.key, set())
        for f in fixable:
            field = f["field"]
            box = Border()
            box.Background = brush("#081221")
            box.CornerRadius = CornerRadius(8)
            box.Padding = Thickness(10, 7, 10, 7)
            box.Margin = Thickness(0, 0, 0, 5)
            sp = StackPanel()
            head = StackPanel()
            head.Orientation = Orientation.Horizontal
            head.Children.Add(text(field, 11.5, "#00A0E3", bold=True))
            head.Children.Add(text(u"now “{}”".format(f.get("value") or "empty"), 11.5, "#8A9BB3",
                                   margin=(8, 0, 0, 0)))
            if field in done:
                head.Children.Add(text(u"✓ fixed", 11.5, "#22C55E", bold=True, margin=(8, 0, 0, 0)))
            sp.Children.Add(head)
            g = Grid()
            g.Margin = Thickness(0, 5, 0, 0)
            for w in (GridLength(1, GridUnitType.Star), GridLength.Auto, GridLength.Auto):
                cd = ColumnDefinition()
                cd.Width = w
                g.ColumnDefinitions.Add(cd)
            value = TextBox()
            value.Text = f.get("suggestion") or f.get("value") or ""
            value.ToolTip = "The value to set" + (" (BIMPulse's suggestion)" if f.get("suggestion") else "")
            g.Children.Add(value)
            results = StackPanel()
            prefix = uniclass_prefix(field)
            if prefix:
                find = Button()
                find.Content = "Find code"
                find.Margin = Thickness(6, 0, 0, 0)
                find.ToolTip = "Search the Uniclass {} table for what you typed".format(prefix)
                find.Click += (lambda v, p, r: lambda s, a: self.find_code(v, p, r))(value, prefix, results)
                Grid.SetColumn(find, 1)
                g.Children.Add(find)
            apply_ = Button()
            apply_.Content = "Apply"
            apply_.Margin = Thickness(6, 0, 0, 0)
            apply_.IsEnabled = self._can_review(t)
            apply_.Click += (lambda fld, v: lambda s, a: self.fix(t, fld, v.Text or ""))(field, value)
            Grid.SetColumn(apply_, 2)
            g.Children.Add(apply_)
            sp.Children.Add(g)
            similar = plug.similar_issues(self.topics, t, field) if field not in done else []
            if similar:
                many = Button()
                many.Content = u"Apply to all {} like this".format(len(similar) + 1)
                many.HorizontalAlignment = HorizontalAlignment.Left
                many.Margin = Thickness(0, 6, 0, 0)
                many.Padding = Thickness(10, 4, 10, 4)
                many.IsEnabled = self._can_review(t)
                many.ToolTip = (u"{} other open issue{} fail{} {} the same way on the same family and type. "
                                u"Shows them first, then sets them all in one undoable change.").format(
                    len(similar), "" if len(similar) == 1 else "s", "s" if len(similar) == 1 else "", field)
                many.Click += (lambda fld, v, others: lambda s, a: self.fix_many(t, others, fld, v.Text or ""))(
                    field, value, similar)
                sp.Children.Add(many)
            sp.Children.Add(results)
            box.Child = sp
            self.spFix.Children.Add(box)

    def find_code(self, value, prefix, results):
        query = (value.Text or "").strip()
        if self.session is None:
            self.status("Sign in to BIMPulse to search Uniclass codes.", error=True)
            return
        if len(query) < 2:
            self.status("Type part of a code or a title first, for example 'doorsets'.", error=True)
            return
        session = self.session

        def done(codes):
            results.Children.Clear()
            if not codes:
                results.Children.Add(text("No codes match.", 11.5, "#8A9BB3", margin=(0, 4, 0, 0)))
            for c in codes[:12]:
                row = text(u"{}  {}".format(c["code"], c["title"]), 12, "#E6EEF7", wrap=True, margin=(0, 4, 0, 0))
                row.Cursor = Cursors.Hand
                row.ToolTip = "Use this code"
                row.MouseLeftButtonUp += (lambda code: lambda s, a: (setattr(value, "Text", code),
                                                                    results.Children.Clear()))(c["code"])
                results.Children.Add(row)
        self.background(lambda: session.uniclass(prefix, query), done)

    def fix(self, t, field, value):
        if not self._can_review(t):
            self.status(VIEWER_NOTE, error=True)
            return
        value = value.strip()

        def job(uidoc, finder):
            res = finder.resolve(t)
            if not res.hits:
                self.status("None of this issue's elements are in this model. Is this the right model?", error=True)
                return
            record = []
            changed, problems = rv.apply_fix(uidoc.Document, res.hits, field, value, record)
            finder.cache.pop(t.key, None)
            if not changed:
                self.status(u"Nothing changed: {}".format("; ".join(problems[:3]) or "no element took the value"),
                            error=True)
                return
            self.fixed.setdefault(t.key, set()).add(field)
            note = u"Fixed in Revit: {} set to “{}” on {} {}.".format(
                field, value, changed, "item" if changed == 1 else "items")
            if problems:
                note += u" Not changed: {}".format("; ".join(problems[:3]))
            fixable = set(f["field"] for f in self._fixable(t))
            all_done = fixable <= self.fixed[t.key] and not problems
            done_text = u"{} set to \u201c{}\u201d on {} element{}".format(
                field, value, changed, "" if changed == 1 else "s")
            resolving = bool(t.source and t.source.cloud and self.session is not None and all_done
                             and t.review == bcf.OPEN)
            self.remember_fix(uidoc.Document, record, field, value, [t], [t] if resolving else [])
            if t.source and t.source.cloud and self.session is not None:
                if all_done and t.review == bcf.OPEN:
                    t.review = bcf.RESOLVED
                    self._post(t, cloud.event_payload(t, "status", bcf.RESOLVED, note),
                               u"Fixed: {}. Marked resolved in BIMPulse.".format(done_text), good=True)
                else:
                    self._post(t, cloud.event_payload(t, "comment", body=note),
                               u"Fixed: {}. Logged on the issue in BIMPulse.".format(done_text), good=True)
            else:
                self.announce("BIMPulse: fixed", u"Fixed: {}.{}".format(
                    done_text, " Mark it resolved when you're happy." if all_done else ""))
            self.refresh(keep=t)
        self.run(job)

    def fix_many(self, t, others, field, value):
        """Sets the value on this issue's elements and every similar issue's, in one undoable
        change; issues with nothing else to fix are marked resolved in BIMPulse together."""
        value = value.strip()
        group = [t] + [x for x in others if x.review == bcf.OPEN]
        if any(not self._can_review(x) for x in group):
            self.status(VIEWER_NOTE, error=True)
            return
        listed = u"\n".join(u"\u2022 " + (x.title or "Issue")[:90] for x in group[:12])
        if len(group) > 12:
            listed += u"\n\u2026 and {} more".format(len(group) - 12)
        if not forms.alert(u"Set {} to \u201c{}\u201d on these {} issues' elements?\n\n{}\n\nOne undoable change in "
                           u"Revit. Issues with nothing else to fix are marked resolved in BIMPulse.".format(
                               field, value, len(group), listed),
                           title=u"BIMPulse: fix {} issues".format(len(group)), yes=True, no=True):
            return
        self.status(u"Fixing {} issues...".format(len(group)))

        def job(uidoc, finder):
            groups = []
            for x in group:
                hits = finder.resolve(x).hits
                if hits:
                    groups.append((x.key, hits))
            if not groups:
                self.status("None of these issues' elements are in this model. Is this the right model?", error=True)
                return
            record = []
            changed, problems = rv.apply_fix_many(uidoc.Document, groups, field, value, record)
            fixed = [x for x in group if changed.get(x.key)]
            for x in group:
                finder.cache.pop(x.key, None)
            if not fixed:
                self.status(u"Nothing changed: {}".format("; ".join(problems[:3]) or "no element took the value"),
                            error=True)
                return
            for x in fixed:
                self.fixed.setdefault(x.key, set()).add(field)
            done = [x for x in fixed if set(f["field"] for f in self._fixable(x)) <= self.fixed[x.key]]
            n = sum(changed.values())
            msg = u"Fixed {} issues: {} set to \u201c{}\u201d on {} element{}.".format(
                len(fixed), field, value, n, "" if n == 1 else "s")
            if problems:
                msg += u" Not changed: {}".format("; ".join(problems[:2]))
            online = [x for x in done if x.source and x.source.cloud and x.issue_key]
            self.remember_fix(uidoc.Document, record, field, value, fixed,
                              online if self.session is not None else [])
            if online and self.session is not None:
                note = u"Fixed in Revit: {} set to \u201c{}\u201d (with {} similar issue{}).".format(
                    field, value, len(fixed) - 1, "" if len(fixed) == 2 else "s")
                bodies, session = cloud.bulk_payload(online, bcf.RESOLVED, note), self.session

                def work():
                    return [e for a, items in bodies.items() for b in items for e in session.bulk(a, b)]

                def saved(events):
                    for x in online:
                        x.review = bcf.RESOLVED
                    self.announce("BIMPulse: fixed", msg + u" {} marked resolved in BIMPulse.".format(len(online)))
                    self.refresh(keep=t)
                self.background(work, saved)
            else:
                self.announce("BIMPulse: fixed", msg)
            self.refresh(keep=t)
        self.run(job)

    # ---------- undo the last fix ----------

    def remember_fix(self, doc, record, field, value, topics, resolved):
        """Keeps what a fix changed so "Undo fix" (or Ctrl+Z here) can put it back."""
        if not record:
            return
        self.last_fix = {"doc": doc.PathName or doc.Title, "record": record, "field": field, "value": value,
                         "topics": topics, "resolved": resolved}
        self.btnUndoFix.Visibility = Visibility.Visible
        self.btnUndoFix.ToolTip = u"Put {} back as it was on {} issue{} (Ctrl+Z)".format(
            field, len(topics), "" if len(topics) == 1 else "s")

    def undo_fix(self):
        fix = self.last_fix
        if fix is None:
            return
        n = len(fix["topics"])

        def job(uidoc, finder):
            doc = uidoc.Document
            if (doc.PathName or doc.Title) != fix["doc"]:
                self.status("That fix was made in another model: open it to undo the fix.", error=True)
                return
            restored, problems = rv.undo_fix(doc, fix["record"])
            if not restored:
                self.status(u"Could not undo the fix: {}".format("; ".join(problems[:3]) or "nothing to put back"),
                            error=True)
                return
            self.last_fix = None
            self.btnUndoFix.Visibility = Visibility.Collapsed
            for t in fix["topics"]:
                finder.cache.pop(t.key, None)
                self.fixed.get(t.key, set()).discard(fix["field"])
            note = u"Fix undone in Revit: {} put back to its previous value.".format(fix["field"])
            reopen = [t for t in fix["resolved"] if t.review == bcf.RESOLVED]
            logged = [t for t in fix["topics"] if t not in reopen and t.source and t.source.cloud and t.issue_key]
            for t in reopen:
                t.review = bcf.OPEN
            if self.session is not None:
                for audit_id, items in cloud.bulk_payload(reopen, bcf.OPEN, note).items():
                    for body in items:
                        self.send_or_keep("bulk", audit_id, body, lambda events: None, queued=self.refresh)
                if len(logged) == 1:  # one issue: say so on it (many: the reopened ones carry the note)
                    t = logged[0]
                    self.send_or_keep("event", t.source.cloud["audit_id"], cloud.event_payload(t, "comment", body=note),
                                      lambda events: None)
            msg = u"Undone: {} put back on {} issue{}.".format(fix["field"], n, "" if n == 1 else "s")
            if reopen:
                msg += u" {} reopened in BIMPulse.".format(len(reopen))
            if problems:
                msg += u" Not put back: {}".format("; ".join(problems[:2]))
            self.announce("BIMPulse: fix undone", msg)
            self.refresh(keep=self.current)
        self.run(job)

    # ---------- the selection's issues ----------

    def toggle_selection(self):
        if self.selection is not None:
            self.set_selection(None)
            return
        self.read_selection()
        try:  # Revit 2023 and later: follow the selection as it changes
            if self._selection_handler is None:
                self._selection_handler = lambda s, a: self.selection is not None and self.read_selection_now()
                HOST_APP.uiapp.SelectionChanged += self._selection_handler
        except Exception:
            self._selection_handler = None

    def read_selection(self):
        self.run(lambda uidoc, finder: self.read_selection_now(uidoc.Document, uidoc))

    def read_selection_now(self, doc=None, uidoc=None):
        """(In Revit's API context.) Shows only the issues of the elements selected in Revit."""
        uidoc = uidoc or HOST_APP.uiapp.ActiveUIDocument
        if uidoc is None:
            return
        doc = uidoc.Document
        ids, uniques = set(), set()
        for eid in uidoc.Selection.GetElementIds():
            ids.add(str(rv.id_value(eid)))
            el = doc.GetElement(eid)
            if el is not None:
                uniques.add(el.UniqueId)
        if not ids:
            self.set_selection(None)
            self.status("Select elements in Revit first, then click 'For selection' (it follows the selection "
                        "in Revit 2023 and later).")
            return
        self.set_selection((ids, uniques))

    def set_selection(self, selection):
        self.selection = selection
        on = selection is not None
        self.btnSelection.Text = u"For selection \u2713" if on else "For selection"
        self.btnSelection.FontWeight = FontWeights.Bold if on else FontWeights.Normal
        self.refresh(keep=self.current)
        if on:
            n = len(self.filtered())
            self.status(u"{} issue{} for the {} selected element{}. Click 'For selection' again to show all.".format(
                n, "" if n == 1 else "s", len(selection[0]), "" if len(selection[0]) == 1 else "s"))

    def poll_activity(self):
        """Fetches new comments and statuses from BIMPulse (the Issues page) for the open audits."""
        self.flush_outbox()
        audits = sorted(set(f.cloud["audit_id"] for f in self.files if f.cloud))
        if self.polling or self.session is None or not audits or not self.IsVisible:
            return
        self.polling = True
        session, since = self.session, dict(self.since)

        def work():
            return dict((a, session.events(a, since=since.get(a))) for a in audits if since.get(a))

        def done(by_audit):
            self.polling = False
            fresh = [e for events in by_audit.values() for e in events]
            if not fresh:
                return
            for a, events in by_audit.items():
                self.since[a] = cloud.newest(events, self.since.get(a))
                self.seen.note_events(events, session.email, a)
            changed = []
            for f in self.files:
                if f.cloud and f.cloud["audit_id"] in by_audit:
                    changed += cloud.apply_events(f, by_audit[f.cloud["audit_id"]])
            t = self.current
            if t is not None and t.source and t.source.cloud and any(
                    e.get("issue_key") == t.issue_key for e in fresh):
                self.load_thread(t)
            if any(e.get("kind") == "assign" for e in fresh):
                self.sync_reviews()  # assignments live on the issue's review: fetch them again
            else:
                self.refresh(keep=t)  # statuses, and the NEW marks
            latest = fresh[0]
            what = latest.get("body") if latest.get("kind") in ("comment", "assign") else "changed a status"
            self.status(u"From BIMPulse: {} \u2014 {}".format(latest.get("author_name") or "Someone",
                                                              (what or "")[:120]))

        def work_safe():
            try:
                return work()
            finally:
                self.polling = False
        self.background(work_safe, done)

    def _snapshot(self, t):
        data = t.snapshot()
        if not data:
            self.bdSnapshot.Visibility = Visibility.Collapsed
            return
        try:
            img = BitmapImage()
            img.BeginInit()
            img.StreamSource = MemoryStream(System.Array[System.Byte](bytearray(data)))
            img.CacheOption = BitmapCacheOption.OnLoad
            img.DecodePixelWidth = 600
            img.EndInit()
            img.Freeze()
            self.imgSnapshot.Source = img
            self.bdSnapshot.Visibility = Visibility.Visible
        except Exception:
            self.bdSnapshot.Visibility = Visibility.Collapsed

    def _elements(self, t, res):
        self.spElements.Children.Clear()
        n = len(t.elements)
        self.txtElementsLabel.Text = "ELEMENTS ({})".format(n) if n else "ELEMENTS"
        if res is None or res.model_wide:
            self.txtModelCheck.Text = ""
        else:
            ok = res.found
            self.txtModelCheck.Text = "{} of {} found in this model".format(ok, len(res.rows))
            self.txtModelCheck.Foreground = brush("#22C55E" if ok == len(res.rows) else ("#F59E0B" if ok else "#EF4444"))
        if not n:
            self.spElements.Children.Add(text("Model-wide issue: there is no element to find. Fix it in the model's "
                                              "settings (for example Project Base Point, Survey Point or levels).",
                                              12.5, "#8A9BB3", wrap=True))
            return
        for i, e in enumerate(t.elements[:200]):
            row = Border()
            row.Background = brush("#081221")
            row.BorderBrush = brush("#16243A")
            row.BorderThickness = Thickness(1)
            row.CornerRadius = CornerRadius(8)
            row.Padding = Thickness(10, 7, 10, 7)
            row.Margin = Thickness(0, 0, 0, 5)
            row.Cursor = Cursors.Hand
            status_label = ""
            if res is not None and i < len(res.rows):
                status_label = ROW_STATUS.get(res.rows[i][0], ("", ""))[0]
            row.ToolTip = hover_card(
                u"{}{}".format(e.get("element_id") and "ID " + str(e["element_id"]) or "Element",
                               u" \u00b7 " + e["category"] if e.get("category") else ""),
                [("Family", e.get("family")), ("Type", e.get("type")), ("Level", e.get("level")),
                 (e.get("parameter") or "Value", e.get("value") if e.get("parameter") else None),
                 ("Unique id", e.get("unique_id")), ("In model", status_label or "Run Check against model")],
                "Click to select and zoom to it in Revit")
            g = Grid()
            c0, c1 = ColumnDefinition(), ColumnDefinition()
            c1.Width = GridLength.Auto
            g.ColumnDefinitions.Add(c0)
            g.ColumnDefinitions.Add(c1)
            sp = StackPanel()
            ident = e.get("element_id") and "ID {}".format(e["element_id"]) or \
                (e.get("level") and "Level {}".format(e["level"])) or \
                (e.get("ifc_guid") and "IFC {}".format(e["ifc_guid"])) or "No id"
            head = StackPanel()
            head.Orientation = Orientation.Horizontal
            head.Children.Add(text(ident, 11.5, "#00A0E3", bold=True, mono=True))
            if e.get("category"):
                head.Children.Add(text(e["category"], 11.5, "#8A9BB3", margin=(10, 0, 0, 0)))
            sp.Children.Add(head)
            ft = " : ".join(x for x in (e.get("family"), e.get("type")) if x)
            if ft:
                sp.Children.Add(text(ft, 12.5, margin=(0, 2, 0, 0)))
            extra = []
            if e.get("level") and e.get("element_id"):
                extra.append("Level " + e["level"])
            if e.get("parameter"):
                extra.append(u"{} = {}".format(e["parameter"], e.get("value") or u"–"))
            if extra:
                sp.Children.Add(text(u"  ·  ".join(extra), 11.5, "#8A9BB3", margin=(0, 2, 0, 0)))
            g.Children.Add(sp)
            if res is not None and i < len(res.rows):
                status, hit, count = res.rows[i]
                label, colour = ROW_STATUS.get(status, ("", "#8A9BB3"))
                if status == "link" and hit is not None:
                    label = hit.where
                elif status == "many":
                    label = "{} matched".format(count)
                p = pill(label.upper(), colour)
                Grid.SetColumn(p, 1)
                g.Children.Add(p)
            row.Child = g
            row.MouseLeftButtonUp += (lambda idx: lambda s, a: self.act("zoom", only=idx))(i)
            self.hoverable(row)
            self.spElements.Children.Add(row)
        if n > 200:
            self.spElements.Children.Add(text("and {:,} more".format(n - 200), 12, "#8A9BB3"))

    # ---------- Revit actions ----------

    def act(self, action, only=None, quiet=False):
        t = self.current
        if t is None or not t.elements:
            return

        def job(uidoc, finder):
            res = finder.resolve(t)
            hits = res.hits
            if only is not None:
                row = res.rows[only] if only < len(res.rows) else None
                hits = [row[1]] if row and row[1] is not None else []
            if self.current is t:
                self._elements(t, res)
            if action == "resolve":
                return
            if not hits:
                if not quiet or only is not None:
                    self.status("Not found in this model or its loaded links. Is this the right model?", error=True)
                else:
                    self.status("{}: not found in this model.".format(t.title))
                return
            if action == "select":
                rv.select(uidoc, hits)
            elif action == "zoom":
                rv.select(uidoc, hits)
                rv.zoom(uidoc, hits)
            elif action == "3d":
                rv.show_3d(uidoc, hits)
            elif action == "isolate":
                rv.isolate(uidoc, hits)
            self.status(u"{} — {} element{} {}.".format(
                t.title, len(hits), "" if len(hits) == 1 else "s",
                {"select": "selected", "zoom": "selected", "3d": "shown in '{}'".format(rv.VIEW_NAME),
                 "isolate": "isolated in the active view"}[action]))
        self.run(job)

    def on_check(self, sender, args):
        topics = [t for t in self.topics if t.elements]
        if not topics:
            return
        self.status("Checking {:,} issues against the model...".format(len(topics)))

        def job(uidoc, finder):
            found = issues = 0
            for t in topics:
                res = finder.resolve(t)
                issues += 1
                if res.found:
                    found += 1
            pct = int(round(100.0 * found / issues)) if issues else 0
            self.statFound.Text = "{}%".format(pct)
            self.statFoundSub.Text = "{:,} of {:,} issues in {}".format(found, issues, uidoc.Document.Title)
            self._fill_list()
            self.status("Checked against {}: {:,} of {:,} issues have elements in this model or its links.".format(
                uidoc.Document.Title, found, issues))
        self.run(job)

    def on_colour(self, sender, args):
        topics = [t for t in self.filtered() if t.elements and t.review == bcf.OPEN]
        if not topics:
            self.status("No open issues with elements to colour.")
            return

        def job(uidoc, finder):
            worst = {}
            for t in topics:
                for h in finder.resolve(t).hits:
                    if h.link:
                        continue
                    k = rv.id_value(h.element.Id)
                    if k not in worst or bcf.SEVERITY_ORDER.get(t.severity, 2) < bcf.SEVERITY_ORDER.get(worst[k][0], 2):
                        worst[k] = (t.severity, h.element.Id)
            by_sev = {}
            for sev, eid in worst.values():
                by_sev.setdefault(sev, []).append(eid)
            done = rv.colour(uidoc, by_sev)
            vid = uidoc.ActiveView.Id.ToString()
            self.coloured.setdefault(vid, []).extend(done)
            self.status("Coloured {:,} elements in '{}' by severity (the issues in the list). "
                        "'Clear colours' puts them back.".format(len(done), uidoc.ActiveView.Name))
        self.run(job)

    def on_clear(self, sender, args):
        def job(uidoc, finder):
            vid = uidoc.ActiveView.Id.ToString()
            ids = self.coloured.pop(vid, [])
            rv.clear(uidoc, ids)
            self.status("Cleared {:,} colour overrides and any isolation in '{}'.".format(len(ids), uidoc.ActiveView.Name))
        self.run(job)

    # ---------- review ----------

    def _can_review(self, t):
        return not (t.source and t.source.cloud) or bool(t.source.cloud.get("can_review"))

    def set_review(self, status):
        t = self.current
        if t is None:
            return
        picked = [it.Tag for it in self.lbIssues.SelectedItems]
        if len(picked) > 1:
            self.bulk_review(picked, status)
            return
        if not self._can_review(t):
            self.status(VIEWER_NOTE, error=True)
            return
        if status == bcf.IGNORED and t.source and t.source.cloud and not (self.tbNote.Text or "").strip():
            self.status(IGNORE_NOTE, error=True)
            self.tbNote.Focus()
            return
        t.review = status
        t.reviewed_by = self.username if status != bcf.OPEN else ""
        t.reviewed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M") if status != bcf.OPEN else ""
        if t.source and t.source.cloud:
            comment = self.tbNote.Text or ""
            self.tbNote.Text = ""
            t.reviewed_by = (self.session.email if self.session else "") or t.reviewed_by
            self._post(t, cloud.event_payload(t, "status", status, comment),
                       "Saved to BIMPulse: your team sees it on the Issues page.",
                       snapshot=bool(self.chkSnapshot.IsChecked))
        else:
            self._save(t)
        nxt = None
        if status != bcf.OPEN and self.cbReview.SelectedIndex == 0:  # it leaves the list: go to the next one
            items = list(self.lbIssues.Items)
            idx = next((i for i, it in enumerate(items) if it.Tag is t), -1)
            rest = items[idx + 1:] + items[:max(idx, 0)]
            nxt = rest[0].Tag if rest else None
        self.refresh(keep=nxt or t)
        if nxt is not None and self.chkAuto.IsChecked and nxt.elements:
            self.act("zoom", quiet=True)

    def bulk_review(self, topics, status):
        """One status for several selected issues: shared ones in one call per result, local ones beside their file."""
        if any(not self._can_review(t) for t in topics):
            self.status(VIEWER_NOTE, error=True)
            return
        reason = (self.tbNote.Text or "").strip()
        online = [t for t in topics if t.source and t.source.cloud and t.issue_key]
        if status == bcf.IGNORED and online and not reason:
            self.status(IGNORE_NOTE, error=True)
            self.tbNote.Focus()
            return
        for t in topics:
            if t not in online:
                t.review = status
                t.reviewed_by = self.username if status != bcf.OPEN else ""
                t.reviewed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M") if status != bcf.OPEN else ""
                self._save(t)
        if not online:
            self.refresh()
            return
        if self.session is None:
            self.status("Sign in to BIMPulse to share these statuses with your team.", error=True)
            return
        bodies, session = cloud.bulk_payload(online, status, reason), self.session

        for t in online:
            t.review = status
        self.tbNote.Text = ""
        word = {bcf.RESOLVED: "marked resolved", bcf.IGNORED: "ignored", bcf.OPEN: "reopened"}[status]

        def done(events):
            self.status("{} issue{} {} in BIMPulse.".format(len(online), "" if len(online) == 1 else "s", word))
            self.refresh()
        for audit_id, items in bodies.items():
            for body in items:
                self.send_or_keep("bulk", audit_id, body, done, queued=self.refresh)

    def on_note(self, sender, args):
        t = self.current
        if t is not None and t.source and t.source.cloud:
            return  # for BIMPulse issues the box is a comment, sent with Send
        if t is not None and (self.tbNote.Text or "") != (t.note or ""):
            if not self._can_review(t):
                self.tbNote.Text = t.note or ""
                self.status(VIEWER_NOTE, error=True)
                return
            t.note = self.tbNote.Text or ""
            self._save(t)

    def _save(self, t):
        """Saves a local file's review beside it (BIMPulse issues go through _post)."""
        try:
            path = bcf.save_review(t.source)
            self.status("Saved to {}".format(os.path.basename(path)))
        except Exception as e:
            self.status("Could not save the review beside the BCF: {}".format(e), error=True)

    def on_copy_ids(self, sender, args):
        t = self.current
        if t is None:
            return
        ids = [e.get("element_id") or e.get("unique_id") for e in t.elements if e.get("element_id") or e.get("unique_id")]
        if ids:
            Clipboard.SetText(", ".join(ids))
            self.status("Copied {} id{} to the clipboard.".format(len(ids), "" if len(ids) == 1 else "s"))

    def on_snapshot(self, sender, args):
        t = self.current
        data = t.snapshot() if t else None
        if not data:
            return
        ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
        path = os.path.join(tempfile.gettempdir(), "bimpulse-issue-{}{}".format(t.guid[:8], ext))
        with open(path, "wb") as f:
            f.write(data)
        os.startfile(path)

    # ---------- BIMPulse account ----------

    def update_account(self):
        if self.session is None:
            self.txtAccount.Text = "Sign in"
            self.txtAvatar.Text = "?"
            self.btnAccount.ToolTip = "Sign in with your BIMPulse account"
            if self.started:
                self.show_start()
            return
        email = self.session.email
        self.txtAccount.Text = email or "Signed in"
        self.txtAvatar.Text = (email[:1] or "?").upper()
        self.btnAccount.ToolTip = "Signed in to BIMPulse as {}. Click to sign out.".format(email)
        if self.started:
            self.show_start()

    def _account(self):
        """The signed-in BIMPulse account (lower-case email), or None."""
        return (self.session.email or "").lower() or None if self.session is not None else None

    def forget_files(self):
        """Closes every open result and forgets them, so the next account starts empty."""
        self.files = []
        self.current = None
        self.since = {}
        self.settings["files"], self.settings["cloud"], self.settings["account"] = [], {}, None
        save_settings(self.settings)
        for finder in self.finders.values():
            finder.cache.clear()
        self.refresh()
        self.status("Click Browse BIMPulse to open a saved audit's issues.")

    def on_account(self, sender, args):
        if self.session is None:
            self.sign_in()
            return
        if forms.alert("Sign out of BIMPulse in Revit?\n\nSigned in as {}.".format(self.session.email),
                       title="BIMPulse", yes=True, no=True):
            session = self.session
            self.session, self.me = None, None
            self.update_account()
            self.show_browse(False)
            self.forget_files()
            self.background(session.sign_out, lambda _: self.status("Signed out of BIMPulse."))

    def sign_in(self, then_browse=False):
        if self.signin is not None:
            self.signin.cancel()

        def finished(session, error):
            def ui():
                self.signin = None
                if session is not None:
                    previous = self.settings.get("account")
                    self.session = session
                    if previous and previous != self._account():
                        self.forget_files()  # another account's results: not for this one
                    self.update_account()
                    self.status("Signed in to BIMPulse as {}.".format(session.email))
                    self.sync_reviews()
                    if then_browse:
                        self.on_browse(None, None)
                elif error:
                    self.status(error, error=True)
            self.Dispatcher.Invoke(System.Action(ui))

        self.signin = cloud.BrowserSignIn(self.settings.get("web_url") or cloud.DEFAULT_WEB_URL, finished)
        try:
            url = self.signin.start()
        except Exception as e:
            self.signin = None
            self.status("Could not start the sign-in: {}".format(e), error=True)
            return
        cloud.open_in_browser(url)
        self.txtAccount.Text = "Signing in…"
        self.status("Finish signing in in your browser, then come back to Revit.")

    def on_closed(self, sender, args):
        self.timer.Stop()
        self.requests.Stop()
        if self._selection_handler is not None:
            try:
                HOST_APP.uiapp.SelectionChanged -= self._selection_handler
            except Exception:
                pass
        if self._theme_handler is not None:
            try:
                HOST_APP.uiapp.ThemeChanged -= self._theme_handler
            except Exception:
                pass
        if self.signin is not None:
            self.signin.cancel()
        save_settings(self.settings)

    # ---------- BIMPulse browser ----------

    def show_browse(self, show=True):
        was = self.pnlBrowse.Visibility == Visibility.Visible
        self.pnlBrowse.Visibility = Visibility.Visible if show else Visibility.Collapsed
        if show and not was:
            animate_in(self.pnlBrowse, 12, 220)

    def on_browse(self, sender, args):
        if self.session is None:
            self.sign_in(then_browse=True)
            return
        self.show_browse(True)
        self.txtBrowseNote.Text = ""
        if not self.projects:
            self.lbProjects.Items.Clear()
            holder = ListBoxItem()
            holder.IsEnabled = False
            holder.Content = skeleton(5, 50)
            holder.Background = brush("#00000000")
            self.lbProjects.Items.Add(holder)
        session = self.session

        def work():
            return session.me(), session.projects()

        def done(result):
            self.me, self.projects = result
            org = (self.me or {}).get("organisation") or {}
            self.txtOrg.Text = org.get("name") or ("Shared with you as a consultant" if session.consultant else "")
            self.fill_projects()
            self.txtBrowseNote.Text = "" if self.projects else "No saved audits yet. Save an audit in BIMPulse first."
        self.background(work, done)

    def on_project_search(self, sender, args):
        self.tbProjectHint.Visibility = Visibility.Collapsed if self.tbProjectSearch.Text else Visibility.Visible
        self.fill_projects()

    def fill_projects(self):
        query = (self.tbProjectSearch.Text or "").strip().lower()
        self.lbProjects.Items.Clear()
        for p in self.projects:
            name = p.get("project_name") or "Untitled project"
            if query and query not in name.lower():
                continue
            item = ListBoxItem()
            item.Tag = p
            body = StackPanel()
            body.Margin = Thickness(12, 9, 12, 9)
            body.Children.Add(text(name, 13, bold=True))
            revs = p.get("revisions") or []
            latest = revs[-1].get("revision") if revs else ""
            bits = [p.get("organisation") or "", "{} revision{}".format(len(revs), "" if len(revs) == 1 else "s"),
                    "latest {}".format(latest) if latest else "", (p.get("last_saved") or "")[:10]]
            body.Children.Add(text(u"  \u00b7  ".join(b for b in bits if b), 11.5, "#8A9BB3", margin=(0, 4, 0, 0)))
            item.Content = body
            self.lbProjects.Items.Add(item)

    def on_project_pick(self, sender, args):
        item = self.lbProjects.SelectedItem
        if item is None:
            return
        p = item.Tag
        self.txtBrowseProject.Text = p.get("project_name") or "Untitled project"
        self.cbRevision.SelectionChanged -= self.on_revision_pick
        self.cbRevision.Items.Clear()
        for r in reversed(p.get("revisions") or []):  # newest first
            label = u"{}  \u00b7  saved {}".format(r.get("revision") or "No revision", (r.get("created_at") or "")[:10])
            self.cbRevision.Items.Add(label)
        self._revisions = list(reversed(p.get("revisions") or []))
        self.cbRevision.SelectionChanged += self.on_revision_pick
        if self._revisions:
            self.cbRevision.SelectedIndex = 0

    def on_revision_pick(self, sender, args):
        i = self.cbRevision.SelectedIndex
        if i < 0 or i >= len(getattr(self, "_revisions", [])):
            return
        audit_id = self._revisions[i]["id"]
        self.spResults.Children.Clear()
        self.spResults.Children.Add(skeleton(4, 48))
        self.txtBrowseNote.Text = ""
        session = self.session

        def done(audit):
            self.browse_audit = audit
            self.fill_results(audit)
        self.background(lambda: session.audit(audit_id), done)

    def fill_results(self, audit):
        self.spResults.Children.Clear()
        open_now = set((f.cloud["audit_id"], f.cloud["module"], f.cloud["consultant"]) for f in self.files if f.cloud)
        by_consultant = {}
        for e in audit.get("entries") or []:
            by_consultant.setdefault(e["consultant"], []).append(e)
        for consultant in sorted(by_consultant):
            self.spResults.Children.Add(text(consultant.upper(), 10.5, "#8A9BB3", mono=True, margin=(0, 10, 0, 6)))
            for e in sorted(by_consultant[consultant], key=lambda x: x["name"]):
                row = Border()
                row.Background = brush("#081221")
                row.BorderBrush = brush("#16243A")
                row.BorderThickness = Thickness(1)
                row.CornerRadius = CornerRadius(8)
                row.Padding = Thickness(12, 8, 12, 8)
                row.Margin = Thickness(0, 0, 0, 6)
                g = Grid()
                for w in (GridLength(1, GridUnitType.Star), GridLength.Auto, GridLength.Auto):
                    cd = ColumnDefinition()
                    cd.Width = w
                    g.ColumnDefinitions.Add(cd)
                cb = CheckBox()
                cb.Tag = e
                cb.Content = text(e["name"], 13, bold=True)
                cb.IsChecked = (audit["id"], e["module"], e["consultant"]) not in open_now
                g.Children.Add(cb)
                reviewed = []
                if e.get("resolved"):
                    reviewed.append("{} resolved".format(e["resolved"]))
                if e.get("ignored"):
                    reviewed.append("{} ignored".format(e["ignored"]))
                note = text(u"  \u00b7  ".join(reviewed), 11.5, "#22C55E", margin=(0, 0, 14, 0))
                note.VerticalAlignment = VerticalAlignment.Center
                Grid.SetColumn(note, 1)
                g.Children.Add(note)
                score = e.get("score")
                if score is not None:
                    colour = "#22C55E" if score >= 80 else ("#F59E0B" if score >= 50 else "#EF4444")
                    p = pill("{}%".format(int(round(float(score)))), colour)
                    Grid.SetColumn(p, 2)
                    g.Children.Add(p)
                row.Child = g
                self.hoverable(row)
                self.spResults.Children.Add(row)
        if not by_consultant:
            self.spResults.Children.Add(text("This saved audit has no results to open.", 12.5, "#8A9BB3"))
        who = "" if audit.get("can_review") else " " + VIEWER_NOTE
        self.txtBrowseNote.Text = (u"{} \u00b7 Rev {} \u00b7 saved {}.".format(
            audit.get("project") or "Project", audit.get("revision") or "-", (audit.get("created_at") or "")[:10])) + who

    def _tick_all(self, on):
        for child in self.spResults.Children:
            if isinstance(child, Border) and isinstance(child.Child, Grid):
                for c in child.Child.Children:
                    if isinstance(c, CheckBox):
                        c.IsChecked = on

    def on_browse_open(self, sender, args):
        audit = self.browse_audit
        if audit is None:
            return
        picked = []
        for child in self.spResults.Children:
            if isinstance(child, Border) and isinstance(child.Child, Grid):
                for c in child.Child.Children:
                    if isinstance(c, CheckBox) and c.IsChecked:
                        picked.append(c.Tag)
        if not picked:
            self.txtBrowseNote.Text = "Tick at least one result to open."
            return
        self.open_results(audit, picked)

    def open_results(self, audit, picked):
        """Downloads saved results ({module, consultant, name, score}) of a saved audit and opens
        them beside the open ones; remembers the audit for the start screen."""
        self.txtBrowseNote.Text = "Downloading {} result{}…".format(len(picked), "" if len(picked) == 1 else "s")
        self.btnBrowseOpen.IsEnabled = False
        self.show_browse(False)
        self.list_loading(True)
        self.status(u"Opening {} result{} from BIMPulse...".format(len(picked), "" if len(picked) == 1 else "s"))
        session, audit_id, can = self.session, audit["id"], bool(audit.get("can_review"))
        can_assign = bool(audit.get("can_assign", can))

        def work():
            got, empty = [], []
            for e in picked:
                path = session.download_bcf(audit_id, e["module"], e["consultant"])
                if path:
                    got.append((path, {"audit_id": audit_id, "module": e["module"], "consultant": e["consultant"],
                                       "can_review": can, "can_assign": can_assign, "score": e.get("score"),
                                       "name": e.get("name")}))
                else:
                    empty.append(u"{} ({})".format(e["name"], e["consultant"]))
            return got, empty

        def done(result):
            self.btnBrowseOpen.IsEnabled = True
            self.list_loading(False)
            got, empty = result
            tags = self._tags()
            fresh = set((t["module"], t["consultant"]) for _, t in got)
            # Opening a result again replaces it; other open files stay.
            keep = [f.path for f in self.files
                    if not (f.cloud and f.cloud["audit_id"] == audit_id and (f.cloud["module"], f.cloud["consultant"]) in fresh)]
            for path, tag in got:
                tags[path] = tag
            if got:
                self.load(keep + [p for p, _ in got], tags)
                self.show_browse(False)
                opened = [f.cloud for f in self.files if f.cloud and f.cloud["audit_id"] == audit_id]
                plug.remember_recent(self.settings, self._account(), {
                    "audit_id": audit_id, "project": audit.get("project") or "Project",
                    "revision": audit.get("revision") or "", "opened_at": datetime.datetime.utcnow().isoformat(),
                    "results": [dict((k, c.get(k)) for k in ("module", "consultant", "name", "score")) for c in opened]})
                save_settings(self.settings)
            note = ("Nothing failed that can be found in a model: " + ", ".join(empty) + ".") if empty else ""
            if not got:
                self.show_browse(True)
                self.txtBrowseNote.Text = note or "Nothing to open."
            elif note:
                self.status(note)
        self.background(work, done)

    # ---------- menu ----------

    def on_menu(self, sender, args):
        menu = ContextMenu()
        style = self.TryFindResource(clr.GetClrType(ContextMenu))
        if style is not None:
            menu.Style = style
        theme = self.settings.get("theme", "auto")

        def item(header, action, checked=None, enabled=True):
            mi = MenuItem()
            mi.Header = header
            mi.IsEnabled = enabled
            if checked is not None:
                mi.IsChecked = checked
            mi.Click += lambda s, a: action()
            menu.Items.Add(mi)

        def line():
            sep = Separator()
            sep_style = self.TryFindResource("MenuSep")
            if sep_style is not None:
                sep.Style = sep_style
            menu.Items.Add(sep)

        item("Theme: match Revit", lambda: self.set_theme("auto"), theme not in ("light", "dark"))
        item("Theme: light", lambda: self.set_theme("light"), theme == "light")
        item("Theme: dark", lambda: self.set_theme("dark"), theme == "dark")
        line()
        item("Open as a window" if self.docked else "Dock beside the model", self.toggle_dock)
        item("Show the tour", self.start_tour)
        item("Keyboard shortcuts", self.show_shortcuts)
        item("Search everything (Ctrl+K)", self.open_command)
        item("Send error reports to BIMPulse", self.toggle_reports, bool(self.settings.get("error_reports")))
        item("Check for updates", lambda: self.check_updates(force=True))
        line()
        item("Help: BIMPulse for Revit", lambda: cloud.open_in_browser(
            (self.settings.get("web_url") or cloud.DEFAULT_WEB_URL).rstrip("/") + "/revit"))
        item("Open the troubleshooting log", self.open_log, enabled=os.path.exists(DEBUG_LOG))
        item(u"Version {}".format(versions().get("plugin_version")), lambda: None, enabled=False)
        self.btnMenu.ContextMenu = menu
        menu.PlacementTarget = self.btnMenu
        menu.Placement = PlacementMode.Bottom
        menu.IsOpen = True

    def open_log(self):
        try:
            os.startfile(DEBUG_LOG)
        except Exception as e:
            self.status("Could not open the log: {}".format(e), error=True)

    def set_theme(self, theme):
        self.settings["theme"] = theme
        save_settings(self.settings)
        shown = current_theme(self.settings)
        if shown == THEME["name"]:
            self.status("Theme saved.")
            return
        self.switch_theme(shown)

    def on_revit_theme(self):
        """Revit's theme changed: follow it straight away (unless a theme was chosen in the menu)."""
        shown = current_theme(self.settings)
        if shown != THEME["name"]:
            self.switch_theme(shown)

    def switch_theme(self, theme):
        if not self.docked:
            self.status("Switching theme...")
            self.reopen()  # a fresh window, drawn in the theme
            return
        self.recolour(theme)

    def recolour(self, theme):
        """Repaints the docked panel in the theme where it stands (it can't be reloaded until
        Revit restarts): every colour on screen and in its resources is swapped."""
        old = THEME["name"]
        if old == theme:
            return
        swap = plug.swap_map(old, theme)
        conv = BrushConverter()

        def mapped(b):
            if not isinstance(b, SolidColorBrush):
                return None
            c = b.Color
            key = "#{:02X}{:02X}{:02X}".format(int(c.R), int(c.G), int(c.B))  # (IronPython: .NET bytes)
            new = swap.get(key)
            if new is None:
                return None
            return "#{:02X}{}".format(int(c.A), new[1:])

        def fix(el, prop):
            try:
                b = getattr(el, prop)
            except Exception:
                return
            new = mapped(b)
            if new is None:
                return
            if b.IsFrozen:
                setattr(el, prop, conv.ConvertFromString(new))
            else:
                b.Color = conv.ConvertFromString(new).Color

        def walk(el):
            for prop in ("Background", "Foreground", "BorderBrush", "Fill", "Stroke", "CaretBrush"):
                fix(el, prop)
            try:
                n = VisualTreeHelper.GetChildrenCount(el)
            except Exception:
                n = 0
            for i in range(n):
                walk(VisualTreeHelper.GetChild(el, i))

        for key in list(self.Resources.Keys):  # the styles' colours (templates use them dynamically)
            value = self.Resources[key]
            new = mapped(value)
            if new is not None:
                self.Resources[key] = conv.ConvertFromString(new)
        walk(self)
        THEME["name"] = theme
        self.refresh(keep=self.current)
        self.status("Theme changed.")

    def reopen(self):
        """Closes this window and opens a fresh one (in Revit's API context, which a new window needs)."""
        save_settings(self.settings)

        def job(uiapp):
            open_window()
            self.Close()
        self.queue.jobs.append(job)
        self.event.Raise()

    def toggle_dock(self):
        """Window -> docked panel (closing the window), or docked panel -> window."""
        save_settings(self.settings)
        pane_id = DockablePaneId(System.Guid(PANEL_ID))

        def job(uiapp):
            if self.docked:
                open_window()
                try:
                    uiapp.GetDockablePane(pane_id).Hide()
                except Exception:
                    pass
                return
            try:
                pane = uiapp.GetDockablePane(pane_id)
            except Exception:
                pane = None
            if pane is None:
                self.status("The docked panel is set up when Revit starts: restart Revit once, then try again.",
                            error=True)
                return
            pane.Show()
            self.Close()
        self.queue.jobs.append(job)
        self.event.Raise()

    def bring_forward(self):
        if self.docked:
            def job(uiapp):
                try:
                    uiapp.GetDockablePane(DockablePaneId(System.Guid(PANEL_ID))).Show()
                except Exception:
                    pass
            self.queue.jobs.append(job)
            self.event.Raise()
            return
        if self.WindowState == WindowState.Minimized:
            self.WindowState = WindowState.Normal
        self.Activate()

    # ---------- first-run tour ----------

    def start_tour(self):
        self.show_browse(False)
        self.tour = [s for s in plug.TOUR if getattr(self, s[0], None) is not None
                     and getattr(self, s[0]).IsVisible]
        if not self.tour:
            return
        self.pnlTour.Visibility = Visibility.Visible
        self.show_tour_step(0)

    def show_tour_step(self, i):
        if i < 0:
            return
        if i >= len(self.tour):
            self.end_tour()
            return
        self.tour_step = i
        name, title, body = self.tour[i]
        target = getattr(self, name)
        W, H = self.rootGrid.ActualWidth, self.rootGrid.ActualHeight
        try:
            p = target.TransformToAncestor(self.rootGrid).Transform(Point(0, 0))
            x, y, w, h = p.X - 6, p.Y - 6, target.ActualWidth + 12, target.ActualHeight + 12
        except Exception:
            x, y, w, h = W / 2, H / 2, 0, 0
        h = min(h, H * 0.6)
        self.pathTourDim.Data = CombinedGeometry(GeometryCombineMode.Exclude, RectangleGeometry(Rect(0, 0, W, H)),
                                                 RectangleGeometry(Rect(x, y, max(w, 0), max(h, 0)), 10, 10))
        self.bdTourRing.Margin = Thickness(x, y, 0, 0)
        self.bdTourRing.Width, self.bdTourRing.Height = max(w, 0), max(h, 0)
        self.txtTourStep.Text = "{} OF {}".format(i + 1, len(self.tour))
        animate_in(self.bdTourCard, 6, 180)
        self.txtTourTitle.Text = title
        self.txtTourBody.Text = body
        self.btnTourBack.IsEnabled = i > 0
        self.btnTourNext.Content = "Done" if i == len(self.tour) - 1 else "Next"
        card = self.bdTourCard
        card.Measure(Size(card.Width, H))
        cw, ch = card.Width, card.DesiredSize.Height
        left = max(12, min(W - cw - 12, x))
        if y + h + ch + 24 < H:
            top = y + h + 12  # below
        elif y - ch - 12 > 0:
            top = y - ch - 12  # above
        else:  # beside, else centred
            top = max(12, min(H - ch - 12, y))
            left = x + w + 12 if x + w + cw + 24 < W else (x - cw - 12 if x - cw - 12 > 0 else (W - cw) / 2)
        card.Margin = Thickness(left, top, 0, 0)

    def end_tour(self):
        self.tour_step = None
        self.pnlTour.Visibility = Visibility.Collapsed
        self.settings["tour_done"] = True
        save_settings(self.settings)

    # ---------- updates ----------

    def check_updates(self, force=False):
        """Once a day (or when asked): is a newer version published? Shows the banner if so."""
        today = datetime.date.today().isoformat()
        local = cloud.installed_version(EXTENSION_DIR)
        if local is None:
            if force:
                self.status("This copy of the plugin wasn't installed by pyRevit, so it can't check for updates.")
            return
        if not force and self.settings.get("update_checked") == today:
            self.show_update(local, self.settings.get("update_latest"))
            return

        def work():
            return cloud.latest_version()

        def done(latest):
            if latest:
                self.settings["update_checked"], self.settings["update_latest"] = today, latest
                save_settings(self.settings)
            if force and latest is None:
                self.status("Could not reach GitHub to check for updates. Try again later.", error=True)
            elif force and latest == local:
                self.status(u"✓ You have the latest version of the BIMPulse tools.", good=True)
            if force and latest and latest != local:
                self.settings.pop("update_dismissed", None)
            self.show_update(local, latest)
        self.quietly(work, done)

    def show_update(self, local, latest):
        new = bool(latest) and latest != local and self.settings.get("update_dismissed") != latest
        self._latest = latest if new else None
        self.bdUpdate.Visibility = Visibility.Visible if new else Visibility.Collapsed
        self.btnUpdateNow.Visibility = Visibility.Visible
        self.txtUpdate.Text = "A new version of the BIMPulse tools for Revit is available."

    def update_later(self):
        if self._latest:
            self.settings["update_dismissed"] = self._latest
            save_settings(self.settings)
        self.bdUpdate.Visibility = Visibility.Collapsed

    def update_now(self):
        """Updates the extension the way pyRevit's Update button does; pyRevit's Reload then
        loads it (this window closes when it reloads)."""
        self.btnUpdateNow.IsEnabled = False
        self.txtUpdate.Text = "Updating..."
        folder = os.path.normcase(os.path.abspath(EXTENSION_DIR))

        def work():
            try:
                from pyrevit.versionmgr import updater
                for repo in updater.get_all_extension_repos():
                    if os.path.normcase(os.path.abspath(repo.directory)) == folder:
                        updater.update_repo(repo)
                        return True
            except Exception:
                debug(u"update failed: {}".format(traceback.format_exc()))
            return False

        def done(ok):
            self.btnUpdateNow.IsEnabled = True
            if ok:
                self.btnUpdateNow.Visibility = Visibility.Collapsed
                self.txtUpdate.Text = ("Updated. Click Reload on the pyRevit tab to start using the new version "
                                       "(the Dashboard closes while pyRevit reloads).")
                self.settings["update_latest"] = cloud.installed_version(EXTENSION_DIR)
                save_settings(self.settings)
            else:
                self.txtUpdate.Text = ("Couldn't update from here. On the pyRevit tab click Update, then Reload. "
                                       "Or see bimpulse.whitecode.co.uk/revit.")
        self.quietly(work, done)

    # ---------- "Open in Revit" from the web ----------

    def poll_requests(self):
        if self.session is None:
            return
        if outdated():  # stops checking in too, so the website doesn't offer it
            self.requests.Stop()
            self.status("BIMPulse for Revit was updated. Restart Revit to update the docked panel"
                        if self.docked else "BIMPulse for Revit was updated. Close and reopen the Dashboard to use it.",
                        error=True)
            return
        self._ticks += 1
        if self._ticks % 3 == 1:
            self.check_in()
        if self.asking:
            return
        self.asking = True
        session = self.session

        def work():
            try:
                return session.next_request(self.session_id)
            finally:
                self.asking = False
        self.quietly(work, lambda req: req and self.open_request(req))

    def check_in(self):
        """Tells BIMPulse this Dashboard is open, with the models open in this Revit (read in
        Revit's API context, then sent in the background)."""
        session = self.session

        def job(uiapp):
            titles = []
            try:
                for doc in uiapp.Application.Documents:
                    if not doc.IsLinked and not doc.IsFamilyDocument:
                        titles.append(doc.Title)
            except Exception:
                pass
            body = plug.session_body(self.session_id, System.Environment.MachineName,
                                     versions().get("revit_version"), self.docked, titles)
            self.quietly(lambda: session.check_in(body))
        self.queue.jobs.append(job)
        self.event.Raise()

    def open_request(self, req):
        """Opens an issue asked for on the web: its result (downloaded if it isn't open), the
        issue in the list, then the comment's viewpoint or its elements."""
        self.bring_forward()
        self.show_browse(False)
        ref = (req["audit_id"], req["module"], req["consultant"])
        if any(f.cloud and (f.cloud["audit_id"], f.cloud["module"], f.cloud["consultant"]) == ref for f in self.files):
            self._focus(req)
            return
        self.status("Opening the issue from BIMPulse...")
        self.list_loading(True)
        session = self.session

        def work():
            return session.audit(req["audit_id"]), session.download_bcf(*ref)

        def done(result):
            self.list_loading(False)
            audit, path = result
            if not path:
                self.status("That result has nothing that can be shown in a model.", error=True)
                return
            can = bool(audit.get("can_review"))
            tags = self._tags()
            tags[path] = {"audit_id": req["audit_id"], "module": req["module"], "consultant": req["consultant"],
                          "can_review": can, "can_assign": bool(audit.get("can_assign", can))}
            self.load([f.path for f in self.files] + [path], tags)
            self._focus(req)
        self.background(work, done)

    def _focus(self, req):
        t = next((t for t in self.topics if t.issue_key == req["issue_key"] and t.source and t.source.cloud
                  and (t.source.cloud["audit_id"], t.source.cloud["module"], t.source.cloud["consultant"])
                  == (req["audit_id"], req["module"], req["consultant"])), None)
        if t is None:
            self.status("That issue is no longer in the saved result.", error=True)
            return
        if t not in self.filtered():  # show every issue so it is in the list
            self.audit_filter = None
            self.tbSearch.Text = ""
            for cb in (self.cbConsultant, self.cbSeverity):
                cb.SelectedIndex = 0
            self.cbReview.SelectedIndex = 1
        self.refresh(keep=t)
        if req.get("viewpoint"):
            self.goto_viewpoint(req["viewpoint"])
        elif t.elements:
            self.act("3d" if req.get("action") == "3d" else "zoom")
        self.status(u"Opened from BIMPulse: {}".format(t.title))

    # ---------- error reports ----------

    def toggle_reports(self):
        self.settings["error_reports"] = not self.settings.get("error_reports")
        save_settings(self.settings)
        self.status("Error reports are {}.".format("on: thank you" if self.settings["error_reports"] else "off"))

    def report_error(self, where, error, detail):
        """Sends an unexpected error to BIMPulse support when the person has said yes (asked once)."""
        choice = self.settings.get("error_reports")
        if choice is None and not self._asked_reports:
            self._asked_reports = True
            choice = bool(forms.alert(
                "Something went wrong in the BIMPulse Dashboard.\n\nSend reports of errors like this to BIMPulse "
                "support, so they can be fixed? Only the error and the plugin and Revit versions are sent: no model "
                "data, file names or comments. You can change this in the menu (⋯) at any time.",
                title="BIMPulse", yes=True, no=True))
            self.settings["error_reports"] = choice
            save_settings(self.settings)
        if not choice or self.session is None:
            return
        body, session = plug.error_report(where, error, detail, versions()), self.session

        def send():
            try:
                session.report_error(body)
            except Exception:
                pass
        t = threading.Thread(target=send)
        t.daemon = True
        t.start()

    def quietly(self, work, done=None):
        """Like background(), but failures are only logged (polling and checks must not nag)."""
        def run():
            try:
                result = work()
            except Exception:
                debug(u"background check failed: {}".format(traceback.format_exc()))
                return
            if done is not None:
                self.Dispatcher.Invoke(System.Action(lambda: done(result)))
        t = threading.Thread(target=run)
        t.daemon = True
        t.start()

    # ---------- hover: glow and a light that follows the cursor ----------

    def hoverable(self, el, part=None, light=True):
        """On hover: a soft BIMPulse-blue glow around `el` and, when `light`, a light that follows
        the cursor across `part` (the card's border; for list rows, their template's border).
        Only the element under the mouse gets them, so long lists stay fast."""
        el.MouseEnter += lambda s, a: self._hover_on(el, part, light)
        if light:
            el.MouseMove += lambda s, a: self._hover_move(el, *self._at(el, a))
        el.MouseLeave += lambda s, a: self._hover_off(el)

    def hoverable_list(self, listbox):
        """The hover effect for a list's rows, with one handler on the list (not one per row: a
        list can have 1,500 rows)."""
        listbox.MouseMove += lambda s, a: self._list_hover(listbox, a)
        listbox.MouseLeave += lambda s, a: getattr(self, "_hover", None) and self._hover_off(self._hover["el"])

    def _list_hover(self, listbox, args):
        el = args.OriginalSource
        while el is not None and not isinstance(el, ListBoxItem) and el is not listbox:
            try:
                el = VisualTreeHelper.GetParent(el)
            except Exception:
                el = None
        hover = getattr(self, "_hover", None)
        if not isinstance(el, ListBoxItem) or not el.IsEnabled:
            if hover:
                self._hover_off(hover["el"])
            return
        if not hover or hover["el"] is not el:
            self._hover_on(el, (lambda it: lambda: it.Template.FindName("bd", it))(el))
        self._hover_move(el, *self._at(el, args))

    def _at(self, el, args):
        target = self._hover_part(el)
        p = args.GetPosition(target) if target is not None else None
        return (p.X, p.Y) if p is not None else (0, 0)

    def _hover_part(self, el):
        hover = getattr(self, "_hover", None)
        return hover["part"] if hover and hover["el"] is el else None

    def _hover_on(self, el, part=None, light=True):
        if getattr(self, "_hover", None):
            self._hover_off(self._hover["el"])
        dark = THEME["name"] == "dark"
        accent = brush("#00A0E3").Color
        glow = DropShadowEffect()
        glow.Color = accent
        glow.ShadowDepth = 0
        glow.Opacity = 0.55 if dark else 0.25
        glow.BlurRadius = 16 if dark else 12
        el.Effect = glow
        if not _reduced_motion():
            grow = DoubleAnimation(0.0, glow.BlurRadius, Duration(System.TimeSpan.FromMilliseconds(140)))
            glow.BeginAnimation(DropShadowEffect.BlurRadiusProperty, grow)
        hover = {"el": el, "part": None}
        target = part() if callable(part) else (part or (el if isinstance(el, Border) else None))
        if light and isinstance(target, Border) and isinstance(target.Background, SolidColorBrush):
            base = target.Background.Color
            line = target.BorderBrush.Color if isinstance(target.BorderBrush, SolidColorBrush) else base
            fill = RadialGradientBrush()
            fill.MappingMode = BrushMappingMode.Absolute
            fill.RadiusX = fill.RadiusY = 160
            fill.GradientStops.Add(GradientStop(_mix(base, accent, 0.16 if dark else 0.10), 0.0))
            fill.GradientStops.Add(GradientStop(base, 1.0))
            edge = RadialGradientBrush()
            edge.MappingMode = BrushMappingMode.Absolute
            edge.RadiusX = edge.RadiusY = 140
            edge.GradientStops.Add(GradientStop(accent, 0.0))
            edge.GradientStops.Add(GradientStop(line, 1.0))
            hover.update(part=target, fill=fill, edge=edge,
                         bg=target.ReadLocalValue(Border.BackgroundProperty),
                         line=target.ReadLocalValue(Border.BorderBrushProperty))
            target.Background, target.BorderBrush = fill, edge
            self._hover = hover
            self._hover_move(el, target.ActualWidth / 2.0, target.ActualHeight / 2.0)
            return
        self._hover = hover

    def _hover_move(self, el, x, y):
        hover = getattr(self, "_hover", None)
        if not hover or hover["el"] is not el or hover.get("fill") is None:
            return
        p = Point(x, y)
        for b in (hover["fill"], hover["edge"]):
            b.Center = p
            b.GradientOrigin = p

    def _hover_off(self, el):
        hover = getattr(self, "_hover", None)
        el.Effect = None
        if not hover or hover["el"] is not el:
            return
        self._hover = None
        target = hover.get("part")
        if target is not None and hover.get("fill") is not None:
            for prop, saved in ((Border.BackgroundProperty, hover["bg"]), (Border.BorderBrushProperty, hover["line"])):
                if saved is DependencyProperty.UnsetValue:
                    target.ClearValue(prop)  # back to the template's colour (and its selected look)
                else:
                    target.SetValue(prop, saved)

    # ---------- health, wins, changes, start ----------

    def draw_ring(self, pct):
        """The resolved ring: a faint track and a green arc for the share resolved."""
        g = self.gridRing
        g.Children.Clear()
        size = g.Width
        th = max(3.0, size / 9.0)
        track = Ellipse()
        track.Stroke = brush("#16243A")
        track.StrokeThickness = th
        g.Children.Add(track)
        if pct <= 0:
            return
        if pct >= 100:
            full = Ellipse()
            full.Stroke = brush("#22C55E")
            full.StrokeThickness = th
            g.Children.Add(full)
            return
        import math
        r, c = (size - th) / 2.0, size / 2.0
        angle = math.radians(360.0 * pct / 100.0)
        start = Point(c, c - r)
        end = Point(c + r * math.sin(angle), c - r * math.cos(angle))
        fig = PathFigure()
        fig.StartPoint = start
        fig.IsClosed = False
        fig.Segments.Add(ArcSegment(end, Size(r, r), 0, pct > 50, SweepDirection.Clockwise, True))
        geo = PathGeometry()
        geo.Figures.Add(fig)
        arc = ShapePath()
        arc.Data = geo
        arc.Stroke = brush("#22C55E")
        arc.StrokeThickness = th
        arc.StrokeStartLineCap = arc.StrokeEndLineCap = System.Windows.Media.PenLineCap.Round
        g.Children.Add(arc)

    def celebrate(self):
        """Every open issue resolved: the ring pops and Windows says well done."""
        if not _reduced_motion():
            grow = ScaleTransform(1, 1)
            self.gridRing.RenderTransformOrigin = Point(0.5, 0.5)
            self.gridRing.RenderTransform = grow
            pop = DoubleAnimation(1.0, 1.25, Duration(System.TimeSpan.FromMilliseconds(260)))
            pop.AutoReverse = True
            ease = CubicEase()
            ease.EasingMode = EasingMode.EaseOut
            pop.EasingFunction = ease
            grow.BeginAnimation(ScaleTransform.ScaleXProperty, pop)
            grow.BeginAnimation(ScaleTransform.ScaleYProperty, pop)
        self.announce("BIMPulse", u"Every issue here is resolved. Brilliant work.")

    def show_wins(self):
        """With results open and no issue picked: the few changes that clear the most issues."""
        wins = plug.biggest_wins(self.topics, NOT_FIXABLE) if self.files and self.win_filter is None else []
        self.pnlWins.Visibility = Visibility.Visible if wins else Visibility.Collapsed
        self.pnlEmpty.Visibility = Visibility.Collapsed if wins else Visibility.Visible
        self.spWins.Children.Clear()
        for n, w in enumerate(wins):
            card = Border()
            card.Background = brush("#081221")
            card.BorderBrush = brush("#1C2B40")
            card.BorderThickness = Thickness(1)
            card.CornerRadius = CornerRadius(10)
            card.Padding = Thickness(12, 10, 12, 10)
            card.Margin = Thickness(0, 0, 0, 8)
            card.Cursor = Cursors.Hand
            g = Grid()
            for width in (GridLength(34), GridLength(1, GridUnitType.Star)):
                cd = ColumnDefinition()
                cd.Width = width
                g.ColumnDefinitions.Add(cd)
            badge = Border()
            badge.Width = badge.Height = 24
            badge.CornerRadius = CornerRadius(12)
            badge.Background = brush("#14304D")
            badge.VerticalAlignment = VerticalAlignment.Top
            num = text(str(n + 1), 12, "#00A0E3", bold=True)
            num.HorizontalAlignment = HorizontalAlignment.Center
            num.VerticalAlignment = VerticalAlignment.Center
            badge.Child = num
            g.Children.Add(badge)
            body = StackPanel()
            Grid.SetColumn(body, 1)
            body.Children.Add(text(w["title"], 13.5, bold=True, wrap=True))
            body.Children.Add(text(w["detail"], 11.5, "#8A9BB3", margin=(0, 3, 0, 0), wrap=True))
            g.Children.Add(body)
            card.Child = g
            self.hoverable(card)
            card.MouseLeftButtonUp += (lambda win: lambda s, a: self.apply_win(win))(w)
            self.spWins.Children.Add(card)
            animate_in(card, 8, 220, n * 50)

    def apply_win(self, w):
        self.win_filter = (set(w["keys"]), "biggest win")
        self.cbReview.SelectedIndex = 0
        self.refresh(animate=True)
        if self.lbIssues.Items.Count:
            self.lbIssues.SelectedIndex = 0
        self.status(u"Showing the {} issues of this win.{} Clear filters to see them all.".format(
            len(w["keys"]), u" In \u2018Fix it in the model\u2019, \u2018Apply to all\u2019 fixes them in one go."
            if w["fix"] else ""))

    def load_changes(self):
        """Since the previous revision: how many issues resolved there are now fixed, and which still fail."""
        refs = sorted(set((f.cloud["audit_id"], f.cloud["module"], f.cloud["consultant"]) for f in self.files if f.cloud))
        if not refs or self.session is None:
            self.spChanged.Visibility = Visibility.Collapsed
            return
        session = self.session

        def work():
            return [(r, session.verification(*r)) for r in refs]

        def done(results):
            prev = next((v.get("previous_revision") for _, v in results if v and v.get("previous_audit_id")), None)
            self.spChanged.Children.Clear()
            if not prev:
                self.spChanged.Visibility = Visibility.Collapsed
                return
            fixed = [x for _, v in results for x in (v.get("verified") or [])]
            failing = set()
            for (a, m, c), v in results:
                keys = set(x["key"] for x in v.get("still_failing") or [])
                failing |= set(t.key for t in self.topics if t.issue_key in keys and t.source and t.source.cloud
                               and (t.source.cloud["audit_id"], t.source.cloud["module"], t.source.cloud["consultant"])
                               == (a, m, c))
            label = text(u"SINCE REV {}".format(prev), 10.5, "#8A9BB3", mono=True, margin=(0, 4, 10, 0))
            self.spChanged.Children.Add(label)
            ok = self._chip(u"\u2713 {} fixed".format(len(fixed)), "#22C55E")
            ok.ToolTip = hover_card(u"Fixed since Rev {}".format(prev), [
                ("", u"\u2022 " + (x.get("title") or "Issue")) for x in fixed[:10]] +
                ([("", u"\u2026 and {} more".format(len(fixed) - 10))] if len(fixed) > 10 else []),
                "Resolved last revision and gone from this one: verified fixed")
            self.spChanged.Children.Add(ok)
            bad = self._chip(u"\u26A0 {} still failing".format(len(failing)), "#EF4444" if failing else "#8A9BB3")
            bad.ToolTip = hover_card(u"Still failing since Rev {}".format(prev), [],
                                     "Marked resolved last revision but failing again. Click to show them.")
            if failing:
                bad.Cursor = Cursors.Hand
                bad.MouseLeftButtonUp += lambda s, a: self.show_keys(failing, "still failing")
            self.spChanged.Children.Add(bad)
            self.spChanged.Visibility = Visibility.Visible
            animate_in(self.spChanged, 4, 200)
        self.quietly(work, done)

    def _chip(self, label, colour):
        chip = Border()
        chip.CornerRadius = CornerRadius(14)
        chip.Padding = Thickness(10, 3, 10, 3)
        chip.Margin = Thickness(0, 0, 6, 6)
        chip.Background = brush("#26" + colour[1:])
        chip.Child = text(label, 11.5, colour, bold=True)
        return chip

    def show_keys(self, keys, label):
        self.win_filter = (set(keys), label)
        self.cbReview.SelectedIndex = 1
        self.refresh(animate=True)
        self.status(u"Showing the {} issues {}. Clear filters to see them all.".format(len(keys), label))

    def show_start(self):
        """Before any result is open: a welcome and the recent audits, one click to reopen."""
        show = not self.files and self.spLoading.Visibility != Visibility.Visible
        was = self.pnlStart.Visibility == Visibility.Visible
        self.pnlStart.Visibility = Visibility.Visible if show else Visibility.Collapsed
        if not show:
            return
        email = self.session.email if self.session is not None else ""
        first = email.split("@")[0].split(".")[0].split("_")[0].title() if email else ""
        self.txtStartTitle.Text = u"Welcome back, {}".format(first) if first else "Welcome to BIMPulse"
        self.btnStartSignIn.Visibility = Visibility.Collapsed if self.session is not None else Visibility.Visible
        recent = plug.recent_for(self.settings, self._account()) if self.session is not None else []
        self.txtStartBody.Text = ("Pick up where you left off, or open another saved audit." if recent else
                                  "Open a saved audit to see its issues, fix them in the model and talk with your team."
                                  if self.session is not None else
                                  "Sign in with your BIMPulse account to open your saved audits.")
        self.lblRecent.Visibility = Visibility.Visible if recent else Visibility.Collapsed
        self.wpRecent.Children.Clear()
        for n, r in enumerate(recent):
            card = Border()
            card.Width = 290 if not self.docked else 300
            card.Margin = Thickness(0, 0, 12, 12)
            card.Padding = Thickness(16, 14, 16, 14)
            card.CornerRadius = CornerRadius(12)
            card.Background = brush("#0B1626")
            card.BorderBrush = brush("#1C2B40")
            card.BorderThickness = Thickness(1)
            card.Cursor = Cursors.Hand
            sp = StackPanel()
            name = text(r.get("project") or "Project", 15.5, bold=True)
            name.FontFamily = DISPLAY_FONT
            sp.Children.Add(name)
            results = r.get("results") or []
            sp.Children.Add(text(u"Rev {} \u00b7 {} result{}".format(r.get("revision") or "-", len(results),
                                                                     "" if len(results) == 1 else "s"),
                                 12, "#8A9BB3", margin=(0, 3, 0, 0)))
            foot = StackPanel()
            foot.Orientation = Orientation.Horizontal
            foot.Margin = Thickness(0, 10, 0, 0)
            score = plug.average(x.get("score") for x in results)
            if score is not None:
                colour = {"Green": "#22C55E", "Amber": "#F59E0B", "Red": "#EF4444"}[plug.grade(score)]
                foot.Children.Add(pill(u"{}%".format(score), colour))
            foot.Children.Add(text(u"Opened {}".format(ago(r.get("opened_at"))), 11.5, "#5D6F88", margin=(8, 1, 0, 0)))
            sp.Children.Add(foot)
            card.Child = sp
            card.ToolTip = hover_card(r.get("project") or "Project",
                                      [("Results", u", ".join(u"{} ({})".format(x.get("name") or x.get("module"),
                                                                                x.get("consultant")) for x in results))],
                                      "Click to open these results again")
            self.hoverable(card)
            card.MouseLeftButtonUp += (lambda entry: lambda s, a: self.reopen_recent(entry))(r)
            self.wpRecent.Children.Add(card)
            if not was:
                animate_in(card, 10, 240, 60 + n * 45)
        if not was:
            animate_in(self.txtStartTitle, 8, 240)

    def reopen_recent(self, entry):
        if self.session is None:
            self.sign_in()
            return
        session = self.session
        wanted = set((x.get("module"), x.get("consultant")) for x in entry.get("results") or [])

        def done(audit):
            picked = [e for e in audit.get("entries") or [] if (e.get("module"), e.get("consultant")) in wanted]
            if not picked:
                self.status("Those results aren't in the saved audit any more. Browse to pick others.", error=True)
                return
            self.browse_audit = audit
            self.open_results(audit, picked)
        self.status(u"Opening {}...".format(entry.get("project") or "the audit"))
        self.background(lambda: session.audit(entry["audit_id"]), done)

    # ---------- look and feel ----------

    def iconise(self):
        """Puts an icon on each main button (keeping its words) and the display font on titles."""
        for name, glyph, after in ICONS:
            b = getattr(self, name, None)
            if b is None:
                continue
            label = b.Content if isinstance(b.Content, basestring) else ""
            label = label.replace(u"\u2039", "").replace(u"\u203a", "").replace(u"\u22ef", "").strip()
            if self.docked and name in ICON_ONLY_DOCKED:
                if label and not b.ToolTip:
                    b.ToolTip = label
                label = ""
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            icon = TextBlock()
            icon.Text = glyph
            icon.FontFamily = ICON_FONT
            icon.FontSize = 13 if label else 14
            icon.VerticalAlignment = VerticalAlignment.Center
            words = None
            if label:
                words = TextBlock()
                words.Text = label
                words.VerticalAlignment = VerticalAlignment.Center
                words.Margin = Thickness(0, 0, 7, 0) if after else Thickness(7, 0, 0, 0)
            for part in ([words, icon] if after else [icon, words]):
                if part is not None:
                    row.Children.Add(part)
            b.Content = row
            self.hoverable(b, light=False)
        if self.docked:
            self.fold_overview()
        for name in ("btnStartBrowse", "btnAssign", "btnBrowseClose", "btnTourNext", "btnTourBack"):
            b = getattr(self, name, None)
            if b is not None:
                self.hoverable(b, light=False)
        for name in ("txtProject", "txtTitle", "statTotal", "statOpen", "statResolved", "statFound", "txtBrowseProject",
                     "txtTourTitle", "pnlNoFilesTitle", "pnlEmptyTitle"):
            el = getattr(self, name, None)
            if el is not None:
                el.FontFamily = DISPLAY_FONT

    def fold_overview(self):
        """Docked panel: the severity bar, audit chips and changes fold away behind "Details", so the
        issue list and the issue get the height (a docked panel is rarely tall)."""
        box = self.gridSeverity.Parent
        details = StackPanel()
        for el in (self.gridSeverity, self.spSeverityKey, self.wpAudits, self.spChanged):
            box.Children.Remove(el)
            details.Children.Add(el)
        toggle = text(u"Details \u25BE", 11.5, "#00A0E3", margin=(0, 8, 0, 0))
        toggle.Cursor = Cursors.Hand
        toggle.ToolTip = "Severity, audits and what changed since the last revision"
        box.Children.Add(toggle)
        box.Children.Add(details)

        def show(on):
            details.Visibility = Visibility.Visible if on else Visibility.Collapsed
            toggle.Text = u"Hide details \u25B4" if on else u"Details \u25BE"
            self.settings["panel_details"] = on
            if on:
                animate_in(details, 6, 180)

        def flip(sender, args):
            show(details.Visibility != Visibility.Visible)
            save_settings(self.settings)
        toggle.MouseLeftButtonUp += flip
        show(bool(self.settings.get("panel_details")))

    def list_loading(self, on):
        """Placeholder rows in the issue list while results download."""
        self.spLoading.Children.Clear()
        if on:
            self.spLoading.Children.Add(skeleton(7, 58))
        self.spLoading.Visibility = Visibility.Visible if on else Visibility.Collapsed
        self.lbIssues.Visibility = Visibility.Collapsed if on else Visibility.Visible
        if on:
            self.pnlNoFiles.Visibility = Visibility.Collapsed

    def _empty_state(self, shown):
        """What the list says when it has nothing to show, and the one thing to do next."""
        if shown or self.spLoading.Visibility == Visibility.Visible:
            self.pnlNoFiles.Visibility = Visibility.Collapsed
            return
        if not self.files:
            glyph, title, body, button, action = (
                u"\uE838", "No issues open yet", "Open a saved audit from BIMPulse to see its issues here.",
                "Browse BIMPulse", lambda: self.on_browse(None, None))
        elif self.cbReview.SelectedIndex == 0 and not any(t.review == bcf.OPEN for t in self.topics):
            glyph, title, body, button, action = (
                u"\uE73E", u"Nothing open \u2014 nice work", "Every issue here is resolved or ignored.",
                "Show all issues", lambda: setattr(self.cbReview, "SelectedIndex", 1))
        else:
            glyph, title, body, button, action = (
                u"\uE721", "No issues match", "Try other filters or search words.", "Clear filters",
                lambda: self.on_clear_filters(None, None))
        self.pnlNoFilesIcon.Text = glyph
        self.pnlNoFilesTitle.Text = title
        self.pnlNoFilesBody.Text = body
        self.pnlNoFilesAction.Content = button
        self._empty_action = action
        if self.pnlNoFiles.Visibility != Visibility.Visible:
            self.pnlNoFiles.Visibility = Visibility.Visible
            animate_in(self.pnlNoFiles, 8, 220)

    # ---------- command bar (Ctrl+K) ----------

    def open_command(self):
        self.show_browse(False)
        self.pnlCommand.Visibility = Visibility.Visible
        self.tbCommand.Text = ""
        self.fill_command()
        animate_in(self.bdCommand, -8, 170)
        self.tbCommand.Focus()

    def close_command(self):
        self.pnlCommand.Visibility = Visibility.Collapsed

    def command_items(self, query):
        """[(glyph, title, detail, run)]: actions, then issues, then projects matching the words."""
        words = [w for w in (query or "").lower().split() if w]

        def hit(text):
            return all(w in text for w in words)
        out = []
        for name, title, keys in COMMANDS:
            b = getattr(self, name, None)
            if b is None or not b.IsEnabled or not b.IsVisible:
                continue
            if words and not hit((title + " " + keys).lower()):
                continue
            key = next((k for k, n, _ in SHORTCUTS if n == name), "")
            glyph = next((g for n, g, _ in ICONS if n == name), u"\uE8A7")
            out.append((glyph, title, key, (lambda btn: lambda: btn.RaiseEvent(
                System.Windows.RoutedEventArgs(Button.ClickEvent)))(b)))
        extras = [(u"\uE7B3", "Issues for the Revit selection", "E", self.toggle_selection),
                  (u"\uE790", "Theme: light", "", lambda: self.set_theme("light")),
                  (u"\uE790", "Theme: dark", "", lambda: self.set_theme("dark")),
                  (u"\uE790", "Theme: match Revit", "", lambda: self.set_theme("auto")),
                  (u"\uE8A0", "Keyboard shortcuts", "F1", self.show_shortcuts),
                  (u"\uE897", "Show the tour", "", self.start_tour)]
        out += [x for x in extras if not words or hit(x[1].lower())]
        if not words:
            out = out[:8]
        issues = [t for t in self.topics if words and hit(t.search_text())][:12]
        for t in issues:
            out.append((u"\uE7BA" if t.review == bcf.OPEN else u"\uE73E", t.title,
                        u" \u00b7 ".join(x for x in (t.audit_name, t.consultant, t.review.title()) if x),
                        (lambda topic: lambda: self.goto_topic(topic))(t)))
        for p in self.projects or []:
            name = p.get("project_name") or ""
            if words and hit(name.lower()):
                out.append((u"\uE838", name, "Project", (lambda proj: lambda: self.goto_project(proj))(p)))
        return out[:30]

    def fill_command(self):
        self.tbCommandHint.Visibility = Visibility.Collapsed if self.tbCommand.Text else Visibility.Visible
        self.lbCommand.Items.Clear()
        for glyph, title, detail, run in self.command_items(self.tbCommand.Text):
            g = Grid()
            for w in (GridLength(28), GridLength(1, GridUnitType.Star), GridLength.Auto):
                cd = ColumnDefinition()
                cd.Width = w
                g.ColumnDefinitions.Add(cd)
            g.Margin = Thickness(10, 8, 10, 8)
            icon = text(glyph, 14, "#00A0E3")
            icon.FontFamily = ICON_FONT
            icon.VerticalAlignment = VerticalAlignment.Center
            g.Children.Add(icon)
            name = text(title, 13, bold=True)
            Grid.SetColumn(name, 1)
            g.Children.Add(name)
            if detail:
                d = text(detail, 11, "#8A9BB3", mono=len(detail) <= 3, margin=(10, 0, 0, 0))
                d.VerticalAlignment = VerticalAlignment.Center
                d.MaxWidth = 220
                Grid.SetColumn(d, 2)
                g.Children.Add(d)
            item = ListBoxItem()
            item.Tag = run
            item.Content = g
            self.lbCommand.Items.Add(item)
        if self.lbCommand.Items.Count:
            self.lbCommand.SelectedIndex = 0
        else:
            empty = ListBoxItem()
            empty.IsEnabled = False
            empty.Content = text("Nothing matches. Try other words.", 12.5, "#8A9BB3", margin=(10, 8, 10, 8))
            self.lbCommand.Items.Add(empty)

    def on_command_key(self, sender, args):
        n = self.lbCommand.Items.Count
        if args.Key == Key.Escape:
            self.close_command()
            args.Handled = True
        elif args.Key in (Key.Down, Key.Up) and n:
            i = self.lbCommand.SelectedIndex + (1 if args.Key == Key.Down else -1)
            self.lbCommand.SelectedIndex = max(0, min(n - 1, i))
            self.lbCommand.ScrollIntoView(self.lbCommand.SelectedItem)
            args.Handled = True
        elif args.Key == Key.Enter:
            self.run_command()
            args.Handled = True

    def run_command(self):
        item = self.lbCommand.SelectedItem
        if item is None or not callable(item.Tag):
            return
        self.close_command()
        item.Tag()

    def goto_topic(self, t):
        """Shows an issue (clearing filters that hide it) and selects its elements."""
        if t not in self.filtered():
            self.audit_filter, self.selection = None, None
            self.tbSearch.Text = ""
            for cb in (self.cbConsultant, self.cbSeverity):
                cb.SelectedIndex = 0
            self.cbReview.SelectedIndex = 1
        self.refresh(keep=t)
        if t.elements:
            self.act("select", quiet=True)

    def goto_project(self, project):
        self.show_browse(True)
        for i in range(self.lbProjects.Items.Count):
            if getattr(self.lbProjects.Items[i], "Tag", None) is project:
                self.lbProjects.SelectedIndex = i
                break

    # ---------- filters and keys ----------

    def on_filter(self, sender, args):
        self.refresh(animate=True)

    def on_search(self, sender, args):
        self.tbSearchHint.Visibility = Visibility.Collapsed if self.tbSearch.Text else Visibility.Visible
        self._fill_list()

    def on_clear_filters(self, sender, args):
        self.audit_filter = None
        self.win_filter = None
        self.selection = None
        self.btnSelection.Text = "For selection"
        self.btnSelection.FontWeight = FontWeights.Normal
        self.tbSearch.Text = ""
        for cb in (self.cbConsultant, self.cbSeverity, self.cbSort):
            cb.SelectedIndex = 0
        self.cbReview.SelectedIndex = 0
        self.refresh()

    def step(self, delta):
        n = self.lbIssues.Items.Count
        if not n:
            return
        i = self.lbIssues.SelectedIndex
        i = 0 if i < 0 else (i + delta) % n
        self.lbIssues.SelectedIndex = i
        self.lbIssues.ScrollIntoView(self.lbIssues.SelectedItem)

    def on_key(self, sender, args):
        mods = Keyboard.Modifiers
        if args.Key == Key.Escape:
            args.Handled = True
            if self.pnlCommand.Visibility == Visibility.Visible:
                self.close_command()
            elif self.tour_step is not None:
                self.end_tour()
            elif self.pnlBrowse.Visibility == Visibility.Visible:
                self.show_browse(False)
            elif not self.docked:
                self.Close()
            return
        if mods == ModifierKeys.Control and args.Key in (Key.Down, Key.Up):
            self.step(1 if args.Key == Key.Down else -1)
            args.Handled = True
            return
        if mods == ModifierKeys.Control and args.Key == Key.Z and self.last_fix is not None \
                and not isinstance(Keyboard.FocusedElement, TextBox):
            self.undo_fix()
            args.Handled = True
            return
        if mods == ModifierKeys.Control and args.Key == Key.K:
            self.open_command()
            args.Handled = True
            return
        if self.pnlCommand.Visibility == Visibility.Visible:
            return
        if mods == ModifierKeys.Control and args.Key == Key.F:
            self.show_browse(False)
            self.tbSearch.Focus()
            self.tbSearch.SelectAll()
            args.Handled = True
            return
        focused = Keyboard.FocusedElement
        if int(mods) != 0 or isinstance(focused, (TextBox, ComboBox)) \
                or self.pnlBrowse.Visibility == Visibility.Visible or self.tour_step is not None:
            return
        key = str(args.Key)
        if key == "E":
            self.toggle_selection()
            args.Handled = True
            return
        if key in ("J", "K"):
            self.step(1 if key == "J" else -1)
            args.Handled = True
        elif key in ("F1", "OemQuestion"):
            self.show_shortcuts()
            args.Handled = True
        elif key in ("D3", "NumPad3"):
            key = "D"
        for k, name, _ in SHORTCUTS:
            if k == key:
                b = getattr(self, name)
                if b.IsEnabled and b.IsVisible:
                    b.RaiseEvent(System.Windows.RoutedEventArgs(Button.ClickEvent))
                args.Handled = True

    def show_shortcuts(self):
        rows = [u"{}\t{}".format(k, what) for k, _, what in SHORTCUTS]
        rows += [u"J / K\tNext / previous issue (also Ctrl+Down / Ctrl+Up)", u"Ctrl+F\tSearch issues",
                 u"Ctrl+Z\tUndo the last fix", u"E\tIssues for the elements selected in Revit",
                 u"Ctrl+K\tFind any issue, project or action",
                 u"Ctrl+Enter\tSend the comment", u"F1 or ?\tThis list"]
        forms.alert(u"Keyboard shortcuts (when you're not typing in a box):\n\n" + u"\n".join(rows),
                    title="BIMPulse Dashboard")


def _logged(name, method):
    """Errors in WPF event handlers are swallowed in Revit: log them, show them in the status bar
    and, when the person has turned error reports on, send them to BIMPulse support."""
    def run(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as e:
            detail = traceback.format_exc()
            debug(u"error in {}: {}".format(name, detail))
            try:
                self.status("Something went wrong ({}). Details are in %APPDATA%\\BIMPulse\\issues-debug.log".format(name),
                            error=True)
            except Exception:
                pass
            try:
                self.report_error(name, e, detail)
            except Exception:
                pass
    run.__name__ = name
    return run


for _name, _method in list(Dashboard.__dict__.items()):
    if callable(_method) and not _name.startswith("__") and _name not in (
            "status", "background", "quietly", "report_error", "setup"):
        setattr(Dashboard, _name, _logged(_name, _method))


# ------------------------------------------------------------------ hosts

class DashboardWindow(forms.WPFWindow, Dashboard):
    """The Dashboard button's window."""

    def __init__(self):
        # Esc is handled by the dashboard: it closes the command bar, tour or browser first.
        forms.WPFWindow.__init__(self, themed_xaml("window.xaml", current_theme()), handle_esc=False)
        # Fit the screen (scaled laptop screens are smaller than the designed 1320 x 840).
        area = SystemParameters.WorkArea
        self.Width = min(self.Width, area.Width - 40)
        self.Height = min(self.Height, area.Height - 40)
        self.MinWidth = min(self.MinWidth, self.Width)
        self.MinHeight = min(self.MinHeight, self.Height)
        self.setup(docked=False)
        self.Closed += self.on_closed
        # The frame: our own title bar and buttons, Windows 11's rounded corners and Mica backdrop.
        self.titleBar.Background = brush("#E6050B14")
        self.btnWinMin.Click += lambda s, a: setattr(self, "WindowState", WindowState.Minimized)
        self.btnWinMax.Click += lambda s, a: self.toggle_maximise()
        self.btnWinClose.Click += lambda s, a: self.Close()
        self.StateChanged += lambda s, a: self.on_window_state()
        self.SourceInitialized += lambda s, a: self.dress_frame()
        self.txtWindowTitle.FontFamily = SANS_FONT
        # The BIMPulse mark (the blue ECG cube) on the taskbar and in Alt+Tab, not pyRevit's icon.
        try:
            icon = BitmapImage()
            icon.BeginInit()
            icon.UriSource = System.Uri(os.path.join(UI_DIR, "logo.png"))
            icon.CacheOption = BitmapCacheOption.OnLoad
            icon.EndInit()
            icon.Freeze()
            self.Icon = icon
        except Exception:
            debug(u"window icon: {}".format(traceback.format_exc()))

    def toggle_maximise(self):
        self.WindowState = WindowState.Normal if self.WindowState == WindowState.Maximized else WindowState.Maximized

    def on_window_state(self):
        big = self.WindowState == WindowState.Maximized
        # A maximised borderless window overhangs the screen by its resize border: keep it on screen.
        self.frameGrid.Margin = Thickness(7) if big else Thickness(0)
        self.btnWinMax.Content = u"" if big else u""
        self.btnWinMax.ToolTip = "Restore" if big else "Maximise"

    def dress_frame(self):
        """Windows 11: rounded corners, a dark or light frame, and the Mica backdrop showing faintly
        through the title bar. Earlier Windows: a solid title bar. Never fails."""
        try:
            import ctypes
            from System.Windows.Interop import HwndSource, WindowInteropHelper
            from System.Windows.Media import Colors
            hwnd = WindowInteropHelper(self).Handle
            handle = ctypes.c_void_p(int(hwnd.ToInt64()))

            def dwm(attribute, value):
                v = ctypes.c_int(value)
                return ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, attribute, ctypes.byref(v), 4) == 0
            dwm(33, 2)  # rounded corners
            dwm(20, 1 if THEME["name"] == "dark" else 0)  # a dark frame for the dark theme
            if System.Environment.OSVersion.Version.Build >= 22621 and dwm(38, 2):  # Mica
                self.chrome.GlassFrameThickness = Thickness(-1)
                HwndSource.FromHwnd(hwnd).CompositionTarget.BackgroundColor = Colors.Transparent
                self.Background = brush("#00000000")
                self.titleBar.Background = brush("#99050B14")
        except Exception:
            debug(u"window frame: {}".format(traceback.format_exc()))


def open_window():
    """Opens the Dashboard window (call it in Revit's API context: a button, or a queued job)."""
    window = DashboardWindow()
    # Modeless windows get no keystrokes from Revit unless WPF's keyboard interop is turned on.
    clr.AddReference("WindowsFormsIntegration")
    from System.Windows.Forms.Integration import ElementHost
    ElementHost.EnableModelessKeyboardInterop(window)
    window.show()
    window.start()
    return window


def register_panel():
    """Registers the docked panel (only possible while Revit starts: the extension's startup.py).
    Its data loads the first time it is shown."""
    class DashboardPanel(forms.WPFPanel, Dashboard):
        panel_id = PANEL_ID
        panel_title = "BIMPulse"
        panel_source = themed_xaml("panel.xaml", current_theme())

        def __init__(self):
            forms.WPFPanel.__init__(self)
            self.setup(docked=True)
            self.IsVisibleChanged += self._first_show

        def _first_show(self, sender, args):
            if self.IsVisible and not self.started:
                self.start()

    return forms.register_dockable_panel(DashboardPanel, default_visible=False)
