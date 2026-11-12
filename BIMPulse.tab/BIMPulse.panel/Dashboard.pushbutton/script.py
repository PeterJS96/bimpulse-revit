# -*- coding: utf-8 -*-
"""Opens BCF issue files exported from BIMPulse (or any BCF tool) in a dashboard:
filter and search the issues, then select, zoom, isolate or show their elements
in 3D, colour them by severity, and mark issues resolved or ignored."""
__title__ = "Issues"
__author__ = "Whitecode Consulting"
__persistentengine__ = True  # the window is modeless and drives Revit through an ExternalEvent

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
from System.Windows import (Clipboard, CornerRadius, FontWeights, GridLength, GridUnitType, HorizontalAlignment,
                            SystemParameters, TextTrimming, TextWrapping, Thickness, VerticalAlignment, Visibility)
from System.Windows.Controls import (Border, Button, CheckBox, ColumnDefinition, Grid, Image, ListBoxItem, Orientation,
                                     StackPanel, TextBlock, TextBox)
from System.Windows.Input import Cursors, Key, Keyboard, ModifierKeys
from System.Windows.Media import BrushConverter, FontFamily
from System.Windows.Media.Imaging import BitmapCacheOption, BitmapImage
from System.Windows.Threading import DispatcherTimer

from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler
from pyrevit import forms

import bimpulse_bcf as bcf
import bimpulse_cloud as cloud
import bimpulse_revit as rv

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
    if hex_colour not in _brushes:
        b = BrushConverter().ConvertFromString(hex_colour)
        b.Freeze()
        _brushes[hex_colour] = b
    return _brushes[hex_colour]


def text(value, size=13, colour="#E6EEF7", bold=False, mono=False, wrap=False, margin=None):
    tb = TextBlock()
    tb.Text = value or ""
    tb.FontSize = size
    tb.Foreground = brush(colour)
    if bold:
        tb.FontWeight = FontWeights.SemiBold
    if mono:
        tb.FontFamily = FontFamily("Consolas")
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


class IssuesWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, os.path.join(os.path.dirname(__file__), "ui.xaml"))
        # Fit the screen (scaled laptop screens are smaller than the designed 1320 x 840).
        area = SystemParameters.WorkArea
        self.Width = min(self.Width, area.Width - 40)
        self.Height = min(self.Height, area.Height - 40)
        self.MinWidth = min(self.MinWidth, self.Width)
        self.MinHeight = min(self.MinHeight, self.Height)
        self.files = []
        self.current = None
        self.audit_filter = None
        self.finders = {}
        self.coloured = {}  # view id -> [ElementId] coloured by this window
        self.settings = load_settings()
        self.username = __revit__.Application.Username  # noqa: F821 (pyRevit global)
        self.session = cloud.Session.load()
        self.signin = None
        self.me = None
        self.projects = []
        self.browse_audit = None
        self.since = {}  # audit id -> newest activity seen
        self.people = []  # the organisation's members, for assigning issues
        self.seen = cloud.Seen()  # which conversations have news since this user last looked
        self.fixed = {}  # topic key -> fields fixed in the model this session
        self.images = {}  # attachment path -> PNG/JPEG bytes
        self._assignee_ids = [None]  # cbAssignee's people, by position
        self.polling = False
        self.timer = DispatcherTimer()
        self.timer.Interval = System.TimeSpan.FromSeconds(LIVE_SECONDS)
        self.timer.Tick += lambda s, a: self.poll_activity()
        self.timer.Start()

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
        self.PreviewKeyDown += self.on_key
        self.Closed += self.on_closed

        self.update_account()
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
        self.refresh()
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
        self.background(work, done)

    # ---------- dashboard ----------

    def refresh(self, keep=None):
        topics = self.topics
        self.pnlNoFiles.Visibility = Visibility.Collapsed if self.files else Visibility.Visible
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
        self._breakdown(topics)
        self._fill_list(keep)

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
            chip.ToolTip = "{} open of {} {} issues".format(n_open, n, name)
            chip.MouseLeftButtonUp += (lambda a: lambda s, e: self.toggle_audit(a))(audit)
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

    def _fill_list(self, keep=None):
        keep = keep or self.current
        shown = self.filtered()
        self.lbIssues.SelectionChanged -= self.on_pick
        self.lbIssues.Items.Clear()
        pick = None
        for t in shown[:LIST_LIMIT]:
            item = ListBoxItem()
            item.Tag = t
            item.Content = self._row(t)
            self.lbIssues.Items.Add(item)
            if keep is not None and t is keep:
                pick = item
        if pick is not None:  # keep the selection without zooming to it again
            self.lbIssues.SelectedItem = pick
            self.lbIssues.ScrollIntoView(pick)
        self.lbIssues.SelectionChanged += self.on_pick
        more = " (showing the first {:,})".format(LIST_LIMIT) if len(shown) > LIST_LIMIT else ""
        self.txtCount.Text = "{:,} ISSUE{}{}".format(len(shown), "" if len(shown) == 1 else "S", more.upper())
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
            who.ToolTip = u"Assigned to {}{}".format(t.assignee_name, ", due " + short_date(t.due_on) if t.due_on else "")
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
        if t is None:
            return
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
                self.txtThreadState.Text = "Loading…"
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
            box.Child = sp
            self.spThread.Children.Add(box)

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

        def send(png=None):
            def work():
                body = dict(payload)
                if png:
                    body["attachment"] = session.attach(audit_id, png)
                    if body["kind"] == "status" and not body.get("body"):
                        body["body"] = "View attached."
                return session.add_event(audit_id, body)
            self.background(work, done)

        if not snapshot:
            send()
            return
        self.chkSnapshot.IsChecked = False
        self.status("Capturing the active view...")

        def job(uidoc, finder):
            try:
                png = rv.snapshot_png(uidoc)
            except Exception as e:
                self.status("Could not capture the view: {}. Sent without it.".format(e), error=True)
                png = None
            send(png)
        self.run(job)

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
        self.background(lambda: session.assign(audit_id, payload), done)

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
            changed, problems = rv.apply_fix(uidoc.Document, res.hits, field, value)
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

    def poll_activity(self):
        """Fetches new comments and statuses from BIMPulse (the Issues page) for the open audits."""
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
            row.ToolTip = "Select and zoom to this element"
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

        def work():
            return [e for audit_id, items in bodies.items() for body in items for e in session.bulk(audit_id, body)]

        def done(events):
            for t in online:
                t.review = status
            self.tbNote.Text = ""
            self.status("{} issue{} {} in BIMPulse ({} changed).".format(
                len(online), "" if len(online) == 1 else "s",
                {bcf.RESOLVED: "marked resolved", bcf.IGNORED: "ignored", bcf.OPEN: "reopened"}[status], len(events)))
            self.refresh()
        self.background(work, done)

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
            return
        email = self.session.email
        self.txtAccount.Text = email or "Signed in"
        self.txtAvatar.Text = (email[:1] or "?").upper()
        self.btnAccount.ToolTip = "Signed in to BIMPulse as {}. Click to sign out.".format(email)

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
        if self.signin is not None:
            self.signin.cancel()
        save_settings(self.settings)

    # ---------- BIMPulse browser ----------

    def show_browse(self, show=True):
        self.pnlBrowse.Visibility = Visibility.Visible if show else Visibility.Collapsed

    def on_browse(self, sender, args):
        if self.session is None:
            self.sign_in(then_browse=True)
            return
        self.show_browse(True)
        self.txtBrowseNote.Text = "Loading your projects…"
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
        self.txtBrowseNote.Text = "Loading the saved audit…"
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
        self.txtBrowseNote.Text = "Downloading {} result{}…".format(len(picked), "" if len(picked) == 1 else "s")
        self.btnBrowseOpen.IsEnabled = False
        session, audit_id, can = self.session, audit["id"], bool(audit.get("can_review"))
        can_assign = bool(audit.get("can_assign", can))

        def work():
            got, empty = [], []
            for e in picked:
                path = session.download_bcf(audit_id, e["module"], e["consultant"])
                if path:
                    got.append((path, {"audit_id": audit_id, "module": e["module"], "consultant": e["consultant"],
                                       "can_review": can, "can_assign": can_assign}))
                else:
                    empty.append(u"{} ({})".format(e["name"], e["consultant"]))
            return got, empty

        def done(result):
            self.btnBrowseOpen.IsEnabled = True
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
            note = ("Nothing failed that can be found in a model: " + ", ".join(empty) + ".") if empty else ""
            if not got:
                self.txtBrowseNote.Text = note or "Nothing to open."
            elif note:
                self.status(note)
        self.background(work, done)

    # ---------- filters and keys ----------

    def on_filter(self, sender, args):
        self.refresh()

    def on_search(self, sender, args):
        self.tbSearchHint.Visibility = Visibility.Collapsed if self.tbSearch.Text else Visibility.Visible
        self._fill_list()

    def on_clear_filters(self, sender, args):
        self.audit_filter = None
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
        if Keyboard.Modifiers == ModifierKeys.Control and args.Key in (Key.Down, Key.Up):
            self.step(1 if args.Key == Key.Down else -1)
            args.Handled = True


def _logged(name, method):
    """Errors in WPF event handlers are swallowed in Revit: log them and show them in the status bar."""
    def run(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            debug(u"error in {}: {}".format(name, traceback.format_exc()))
            try:
                self.status("Something went wrong ({}). Details are in %APPDATA%\\BIMPulse\\issues-debug.log".format(name),
                            error=True)
            except Exception:
                pass
    run.__name__ = name
    return run


for _name, _method in list(IssuesWindow.__dict__.items()):
    if callable(_method) and not _name.startswith("__") and _name not in ("status", "background"):
        setattr(IssuesWindow, _name, _logged(_name, _method))

try:
    window = IssuesWindow()
    # Modeless windows get no keystrokes from Revit unless WPF's keyboard interop is turned on.
    clr.AddReference("WindowsFormsIntegration")
    from System.Windows.Forms.Integration import ElementHost
    ElementHost.EnableModelessKeyboardInterop(window)
    window.show()
except Exception:
    debug("could not open: " + traceback.format_exc())
    forms.alert("The BIMPulse Dashboard could not open.\n\n" + traceback.format_exc(), title="BIMPulse Dashboard")
