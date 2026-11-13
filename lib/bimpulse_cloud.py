# -*- coding: utf-8 -*-
"""BIMPulse online for the Issues dashboard: sign in with your BIMPulse
account, list projects and saved audits, download an audit's issues as BCF,
and share issue statuses with your organisation.

Signing in: the plugin listens on 127.0.0.1 (a free port) and opens the web
app's /connect/revit?port=&state= in the browser. You sign in there as usual
(Microsoft, password, two-factor), and the page posts a session of the
plugin's own back to 127.0.0.1 with the same `state`. The session is kept for
your Windows user only (DPAPI-encrypted in %APPDATA%\\BIMPulse) and refreshed
as needed; "Sign out" ends it in BIMPulse too.

Plain Python (IronPython 2.7 in pyRevit, CPython for the tests). HTTPS goes
through .NET in IronPython (TLS 1.2) and urllib in CPython.
"""
from __future__ import unicode_literals

import base64
import io
import json
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time

try:  # Python 3
    from urllib.parse import parse_qsl, quote, urlencode
except ImportError:  # IronPython 2.7
    from urllib import quote, urlencode
    from urlparse import parse_qsl

DEFAULT_WEB_URL = "https://bimpulse.whitecode.co.uk"
# The plugin's public repository: pyRevit installs and updates it from the master branch.
RELEASES = "https://api.github.com/repos/PeterJS96/bimpulse-revit/commits/master"
CACHE_DAYS = 30  # downloaded results not opened for this long are removed
CONNECT_PATH = "/connect/revit"
LOGIN_TIMEOUT = 300  # seconds to finish signing in in the browser
IRONPYTHON = sys.platform == "cli"
_APPDATA = os.environ.get("APPDATA") or tempfile.gettempdir()
SESSION_FILE = os.path.join(_APPDATA, "BIMPulse", "revit-session.dat")
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "BIMPulse", "bcf-cache")
SESSION_FIELDS = ("access_token", "refresh_token", "expires_at", "email", "supabase_url", "supabase_key",
                  "api_url", "web_url")


class CloudError(Exception):
    """A BIMPulse call failed; the message is safe to show."""

    def __init__(self, message, status=0):
        Exception.__init__(self, message)
        self.status = status


class SignedOut(CloudError):
    pass


# --------------------------------------------------------------- transport

def _dotnet_request(method, url, headers, body):
    import clr  # noqa: F401
    import System
    from System.Net import HttpWebRequest, SecurityProtocolType, ServicePointManager, WebException
    from System.IO import MemoryStream
    try:
        ServicePointManager.SecurityProtocol = ServicePointManager.SecurityProtocol | SecurityProtocolType.Tls12
    except Exception:
        pass
    req = HttpWebRequest.Create(url)
    req.Method = method
    req.Timeout = 60000
    req.ReadWriteTimeout = 120000
    for k, v in headers.items():
        if k.lower() == "content-type":
            req.ContentType = v
        elif k.lower() == "accept":
            req.Accept = v
        elif k.lower() == "user-agent":  # a restricted header in .NET
            req.UserAgent = v
        else:
            req.Headers.Add(k, v)
    if body is not None:
        data = System.Array[System.Byte](bytearray(body))
        req.ContentLength = data.Length
        stream = req.GetRequestStream()
        stream.Write(data, 0, data.Length)
        stream.Close()
    try:
        resp = req.GetResponse()
    except WebException as e:
        resp = e.Response
        if resp is None:
            raise CloudError("BIMPulse could not be reached. Check your connection.")
    try:
        ms = MemoryStream()
        resp.GetResponseStream().CopyTo(ms)
        return int(resp.StatusCode), bytes(bytearray(ms.ToArray()))
    finally:
        resp.Close()


def _urllib_request(method, url, headers, body):
    try:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
    except ImportError:  # pragma: no cover
        from urllib2 import Request, urlopen, HTTPError, URLError
    req = Request(url, data=body, headers=headers)
    req.get_method = lambda: method
    try:
        resp = urlopen(req, timeout=60)
        return resp.getcode(), resp.read()
    except HTTPError as e:
        return e.code, e.read()
    except URLError:
        raise CloudError("BIMPulse could not be reached. Check your connection.")


def default_transport(method, url, headers, body):
    return (_dotnet_request if IRONPYTHON else _urllib_request)(method, url, headers, body)


# --------------------------------------------------------------- session storage

def _protect(data):
    if not IRONPYTHON:
        return data
    import clr
    clr.AddReference("System.Security")
    import System
    from System.Security.Cryptography import DataProtectionScope, ProtectedData
    raw = ProtectedData.Protect(System.Array[System.Byte](bytearray(data)), None, DataProtectionScope.CurrentUser)
    return bytes(bytearray(raw))


def _unprotect(data):
    if not IRONPYTHON:
        return data
    import clr
    clr.AddReference("System.Security")
    import System
    from System.Security.Cryptography import DataProtectionScope, ProtectedData
    raw = ProtectedData.Unprotect(System.Array[System.Byte](bytearray(data)), None, DataProtectionScope.CurrentUser)
    return bytes(bytearray(raw))


def _to_bytes(text):
    return text if isinstance(text, bytes) else text.encode("utf-8")


def _to_text(data):
    try:
        return data.decode("utf-8")
    except (AttributeError, UnicodeError):
        return data


class Session(object):
    """The plugin's own BIMPulse session."""

    def __init__(self, fields, path=SESSION_FILE, transport=None, clock=time.time):
        self.f = dict((k, fields.get(k)) for k in SESSION_FIELDS)
        self.f["expires_at"] = int(float(self.f.get("expires_at") or 0))
        self.path = path
        self.transport = transport or default_transport
        self.clock = clock
        self._lock = threading.Lock()

    @property
    def email(self):
        return self.f.get("email") or ""

    @property
    def web_url(self):
        return (self.f.get("web_url") or DEFAULT_WEB_URL).rstrip("/")

    @property
    def api_url(self):
        return (self.f.get("api_url") or "").rstrip("/")

    # -- storage --

    def save(self):
        folder = os.path.dirname(self.path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        data = _protect(_to_bytes(json.dumps(self.f, ensure_ascii=False)))
        with open(self.path, "wb") as fh:
            fh.write(data)

    @classmethod
    def load(cls, path=SESSION_FILE, transport=None, clock=time.time):
        """The saved session, or None."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as fh:
                fields = json.loads(_to_text(_unprotect(fh.read())))
        except Exception:
            return None
        if not fields.get("refresh_token") or not fields.get("api_url"):
            return None
        return cls(fields, path, transport, clock)

    def forget(self):
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass

    # -- tokens --

    def _auth_call(self, path, body, token=None):
        headers = {"apikey": self.f["supabase_key"], "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.transport("POST", self.f["supabase_url"].rstrip("/") + path, headers,
                              _to_bytes(json.dumps(body, ensure_ascii=False)) if body is not None else None)

    def refresh(self):
        with self._lock:
            status, data = self._auth_call("/auth/v1/token?grant_type=refresh_token",
                                           {"refresh_token": self.f["refresh_token"]})
            if status != 200:
                self.forget()
                raise SignedOut("Your BIMPulse sign-in has ended. Sign in again.", status)
            body = json.loads(_to_text(data))
            self.f["access_token"] = body["access_token"]
            self.f["refresh_token"] = body["refresh_token"]
            self.f["expires_at"] = int(body.get("expires_at") or self.clock() + int(body.get("expires_in") or 3600))
            self.save()

    def token(self):
        if not self.f.get("access_token") or self.f["expires_at"] - 60 < self.clock():
            self.refresh()
        return self.f["access_token"]

    def sign_out(self):
        """Ends this session in BIMPulse (other sign-ins are untouched) and forgets it here."""
        try:
            self._auth_call("/auth/v1/logout?scope=local", None, self.token())
        except CloudError:
            pass
        self.forget()

    # -- API --

    def call(self, method, path, params=None, body=None, raw=False, data=None, content_type=None):
        """One API call. `body` is sent as JSON, or `data` (bytes) as `content_type`."""
        url = self.api_url + path + ("?" + urlencode(params) if params else "")
        for attempt in (0, 1):
            headers = {"Authorization": "Bearer " + self.token(), "Accept": "*/*"}
            payload = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                payload = _to_bytes(json.dumps(body, ensure_ascii=False))
            elif data is not None:
                headers["Content-Type"] = content_type or "application/octet-stream"
                payload = data
            status, data = self.transport(method, url, headers, payload)
            if status == 401 and attempt == 0:
                self.refresh()
                continue
            break
        if status == 401:
            self.forget()
            raise SignedOut("Your BIMPulse sign-in has ended. Sign in again.", status)
        if status >= 400:
            raise CloudError(_detail(data) or "BIMPulse answered {}".format(status), status)
        return data if raw else json.loads(_to_text(data) or "null")

    def me(self):
        data = self.call("GET", "/me")
        # Not a member of an organisation, but shared projects as a design consultant: their
        # own issues only, through /consult.
        self._consultant = bool((data or {}).get("status") != "active" and (data or {}).get("consultant_projects"))
        return data

    @property
    def consultant(self):
        if getattr(self, "_consultant", None) is None:
            self.me()
        return self._consultant

    def _path(self, team, consult):
        return consult if self.consultant else team

    def projects(self):
        return self.call("GET", self._path("/audits/projects", "/consult/projects"))

    def audit(self, audit_id):
        return self.call("GET", self._path("/revit/audits/{}", "/consult/audits/{}").format(audit_id))

    def reviews(self, audit_id):
        return self.call("GET", self._path("/revit/audits/{}/reviews", "/consult/audits/{}/reviews").format(audit_id))

    def events(self, audit_id, since=None, module=None, consultant=None, issue_key=None, limit=None):
        """The audit's activity (comments, status changes, assignments), newest first."""
        params = [(k, _to_bytes(v)) for k, v in (("since", since), ("module", module), ("consultant", consultant),
                                                   ("issue_key", issue_key), ("limit", limit and str(limit))) if v]
        return self.call("GET", self._path("/issues/audits/{}/events", "/consult/audits/{}/events").format(audit_id),
                         params)

    def add_event(self, audit_id, body):
        """A comment, or a status change (with an optional comment), seen live in BIMPulse's Issues page."""
        return self.call("POST", self._path("/issues/audits/{}/events", "/consult/audits/{}/events").format(audit_id),
                         body=body)

    def bulk(self, audit_id, body):
        """One status for many issues of a result (ignoring needs a reason in "body")."""
        return self.call("POST", self._path("/issues/audits/{}/bulk", "/consult/audits/{}/bulk").format(audit_id),
                         body=body)

    def assign(self, audit_id, body):
        """Assigns an issue ({module, consultant, issue_key, title, assignee, due_on, source})."""
        return self.call("PUT", "/issues/audits/{}/assignment".format(audit_id), body=body)

    def people(self):
        """Who issues can be assigned to: [{id, name, email, role}] (nobody, for a consultant)."""
        if self.consultant:
            return []
        return self.call("GET", "/issues/people")

    def attach(self, audit_id, png):
        """Uploads a PNG (a view snapshot) for a comment; returns its path."""
        return self.call("POST", self._path("/issues/audits/{}/attachments",
                                            "/consult/audits/{}/attachments").format(audit_id), data=png,
                         content_type="image/png")["path"]

    def attachment(self, path):
        """An image sent with a comment (bytes)."""
        return self.call("GET", self._path("/issues/attachments", "/consult/attachments"),
                         [("path", _to_bytes(path))], raw=True)

    def verification(self, audit_id, module, consultant):
        """The previous revision's resolved issues on this result: {previous_revision, verified
        (gone: fixed), still_failing}."""
        return self.call("GET", self._path("/issues/audits/{}/verification",
                                           "/consult/audits/{}/verification").format(audit_id),
                         [("module", module), ("consultant", _to_bytes(consultant))])

    def next_request(self):
        """The newest "Open in Revit" asked for on the web in the last two minutes (claimed, so
        only one Revit window gets it), or None: {audit_id, module, consultant, issue_key, viewpoint}."""
        return self.call("GET", self._path("/issues/revit-requests/next", "/consult/revit-requests/next"))

    def report_error(self, body):
        """Sends an error report to BIMPulse support (only when the person turned them on)."""
        return self.call("POST", "/revit/error-reports", body=body)

    def uniclass(self, prefix, query):
        """Uniclass 2015 codes matching the query: [{code, title}]."""
        return self.call("GET", self._path("/revit/uniclass", "/consult/uniclass"),
                         [("prefix", prefix), ("q", _to_bytes(query))])

    def download_bcf(self, audit_id, module, consultant, folder=CACHE_DIR):
        """Saves one result's issues as a .bcfzip in the cache; returns its path (or None: no issues)."""
        try:
            data = self.call("GET", self._path("/revit/audits/{}/bcf", "/consult/audits/{}/bcf").format(audit_id),
                             {"module": module, "consultant": _to_bytes(consultant)}, raw=True)
        except CloudError as e:
            if e.status == 422:
                return None
            raise
        target = os.path.join(folder, audit_id)
        if not os.path.isdir(target):
            os.makedirs(target)
        path = os.path.join(target, "{}--{}.bcfzip".format(module, safe_name(consultant)))
        with open(path, "wb") as fh:
            fh.write(data)
        return path


def _detail(data):
    try:
        body = json.loads(_to_text(data))
    except Exception:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("message") or ""
    if isinstance(detail, list):
        return "; ".join(d.get("msg", "") for d in detail if isinstance(d, dict))
    return detail or ""


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text or "").strip("_")[:60] or "result"


# --------------------------------------------------------------- browser sign-in

def new_state():
    raw = bytearray(random.SystemRandom().getrandbits(8) for _ in range(24))
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")


def parse_request(data):
    """(method, path, headers, body) of a raw HTTP request, or None if incomplete."""
    text = data.decode("latin-1") if not isinstance(data, type("")) else data
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        return None
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length") or 0)
    if len(body) < length:
        return None
    return parts[0].upper(), parts[1], headers, body[:length]


def parse_form(body):
    """application/x-www-form-urlencoded -> dict of text values."""
    out = {}
    for k, v in parse_qsl(body, keep_blank_values=True):
        if not IRONPYTHON and isinstance(v, bytes):  # pragma: no cover
            v = v.decode("utf-8")
        try:  # IronPython 2: the percent-decoded value is UTF-8 bytes in a str
            v = v.encode("latin-1").decode("utf-8") if IRONPYTHON else v
        except (UnicodeError, AttributeError):
            pass
        out[k] = v
    return out


PAGE = ("<!doctype html><html><head><meta charset='utf-8'><title>BIMPulse</title><style>"
        "body{{background:#050b14;color:#e6eef7;font-family:Segoe UI,sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:100vh;margin:0}}div{{background:#0b1626;border:1px solid #1c2b40;"
        "border-top:3px solid {0};border-radius:12px;padding:28px 32px;max-width:420px}}"
        "h1{{font-size:18px;margin:0 0 8px}}p{{color:#8a9bb3;margin:0}}</style></head><body><div><h1>{1}</h1>"
        "<p>{2}</p></div></body></html>")


def _respond(conn, status, title, message, colour="#00a0e3"):
    html = PAGE.format(colour, title, message).encode("utf-8")
    head = "HTTP/1.1 {}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n" \
           "Cache-Control: no-store\r\nConnection: close\r\n\r\n".format(status, len(html))
    try:
        conn.sendall(head.encode("ascii") + html)
    except socket.error:
        pass


class BrowserSignIn(object):
    """Waits on 127.0.0.1 for the web app to post the plugin's session.

    start() returns the URL to open; on_done(session, error) runs on the
    listener's thread when it finishes (one of them is None).
    """

    def __init__(self, web_url=DEFAULT_WEB_URL, on_done=None, session_path=SESSION_FILE, transport=None,
                 timeout=LOGIN_TIMEOUT):
        self.web_url = (web_url or DEFAULT_WEB_URL).rstrip("/")
        self.on_done = on_done
        self.session_path = session_path
        self.transport = transport
        self.timeout = timeout
        self.state = new_state()
        self.sock = None
        self.cancelled = False
        self.port = 0

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(1.0)
        self.port = self.sock.getsockname()[1]
        t = threading.Thread(target=self._serve)
        t.daemon = True
        t.start()
        return "{}{}?port={}&state={}".format(self.web_url, CONNECT_PATH, self.port, quote(self.state))

    def cancel(self):
        self.cancelled = True

    def _finish(self, session, error):
        try:
            self.sock.close()
        except Exception:
            pass
        if self.on_done:
            self.on_done(session, error)

    def _serve(self):
        deadline = time.time() + self.timeout
        while not self.cancelled and time.time() < deadline:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except socket.error:
                break
            try:
                session = self._handle(conn)
            except Exception as e:  # keep listening: the next try may work
                session = None
                _respond(conn, "500 Internal Server Error", "Sign-in failed",
                         "Revit could not save the sign-in ({}). Try again.".format(e), "#ef4444")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            if session is not None:
                self._finish(session, None)
                return
        self._finish(None, None if self.cancelled else "Sign-in timed out. Click Sign in to try again.")

    def _handle(self, conn):
        conn.settimeout(10.0)
        data = b""
        parsed = None
        try:
            while parsed is None and len(data) < 65536:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                data += chunk
                parsed = parse_request(data)
        except socket.error:
            return None
        if parsed is None:
            return None
        method, path, headers, body = parsed
        if method != "POST" or path.split("?")[0] != "/callback":
            _respond(conn, "404 Not Found", "Nothing here", "This address only receives the BIMPulse sign-in.")
            return None
        form = parse_form(body)
        if form.get("state") != self.state or not form.get("refresh_token") or not form.get("api_url"):
            _respond(conn, "400 Bad Request", "Sign-in not accepted",
                     "This sign-in did not come from the Revit window that asked for it. "
                     "Click Sign in in Revit again.", "#ef4444")
            return None
        session = Session(form, self.session_path, self.transport)
        session.save()
        _respond(conn, "200 OK", "Revit is connected",
                 "Signed in as {}. You can close this tab and go back to Revit.".format(
                     (form.get("email") or "your account").replace("<", "")), "#22c55e")
        return session


def open_in_browser(url):
    try:
        if IRONPYTHON:
            import System
            System.Diagnostics.Process.Start(url)
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception:
        os.startfile(url)


# --------------------------------------------------------------- reviews

def result_ref(topic):
    """{"module", "consultant"} of a topic from BIMPulse, or None."""
    cloud = getattr(topic.source, "cloud", None) if topic.source else None
    if not cloud or not topic.issue_key:
        return None
    return {"module": cloud["module"], "consultant": cloud["consultant"]}


def bulk_payload(topics, status, body=None):
    """POST /issues/audits/{id}/bulk bodies for topics from BIMPulse, grouped by audit and
    result: {audit_id: [body, ...]} (at most 500 issues each)."""
    out = {}
    groups = {}
    for t in topics:
        ref = result_ref(t)
        if ref is None:
            continue
        audit_id = t.source.cloud["audit_id"]
        groups.setdefault((audit_id, ref["module"], ref["consultant"]), []).append(t)
    for (audit_id, module, consultant), ts in sorted(groups.items()):
        for start in range(0, len(ts), 500):
            out.setdefault(audit_id, []).append({
                "module": module, "consultant": consultant, "status": status, "source": "revit",
                "body": (body or "").strip() or None,
                "issues": [{"issue_key": t.issue_key, "title": (t.title or "")[:300] or None}
                           for t in ts[start:start + 500]]})
    return out


def assign_payload(topic, assignee, due_on):
    """PUT /issues/audits/{id}/assignment body (due_on "YYYY-MM-DD" or None), or None."""
    ref = result_ref(topic)
    if ref is None:
        return None
    return dict(ref, issue_key=topic.issue_key, title=(topic.title or "")[:300] or None,
                assignee=assignee or None, due_on=due_on or None, source="revit")


def event_payload(topic, kind, status=None, body=None, attachment=None, viewpoint=None):
    """The POST /issues/audits/{id}/events body for a topic from BIMPulse, or None
    (a local file, or an older export without issue keys)."""
    cloud = getattr(topic.source, "cloud", None) if topic.source else None
    if not cloud or not topic.issue_key:
        return None
    out = {"module": cloud["module"], "consultant": cloud["consultant"], "issue_key": topic.issue_key,
           "kind": kind, "status": status, "body": (body or "").strip() or None,
           "title": (topic.title or "")[:300] or None, "source": "revit"}
    if attachment:
        out["attachment"] = attachment
    if viewpoint:
        out["viewpoint"] = viewpoint
    return out


def _when(iso):
    return (iso or "")[:16].replace("T", " ")


def apply_events(bcf_file, events):
    """Applies status changes from the activity (newest first) to the file's topics.
    Returns the topics that changed."""
    cloud = getattr(bcf_file, "cloud", None) or {}
    by_key = dict((t.issue_key, t) for t in bcf_file.topics if t.issue_key)
    changed = []
    for e in reversed(events or []):
        if e.get("kind") != "status" or e.get("module") != cloud.get("module") \
                or e.get("consultant") != cloud.get("consultant"):
            continue
        t = by_key.get(e.get("issue_key"))
        if t is not None and e.get("status"):
            t.review = e["status"]
            t.reviewed_by = e.get("author_name") or ""
            t.reviewed_at = _when(e.get("created_at"))
            if t not in changed:
                changed.append(t)
    return changed


def newest(events, current=None):
    """The latest created_at among events (and `current`), for the next `since`."""
    times = [e.get("created_at") or "" for e in events or []] + ([current] if current else [])
    return max(times) if times else None


def apply_reviews(bcf_file, reviews):
    """Sets topics' review statuses from GET /revit/audits/{id}/reviews."""
    cloud = getattr(bcf_file, "cloud", None) or {}
    by_key = dict((r.get("issue_key"), r) for r in reviews or []
                  if r.get("module") == cloud.get("module") and r.get("consultant") == cloud.get("consultant"))
    for t in bcf_file.topics:
        r = by_key.get(t.issue_key)
        if r:
            t.review = r.get("status") or t.review
            t.note = r.get("note") or ""
            t.reviewed_by = r.get("updated_by_name") or ""
            t.reviewed_at = _when(r.get("updated_at"))
        t.assignee = (r or {}).get("assignee")
        t.assignee_name = (r or {}).get("assignee_name") or ""
        t.due_on = str((r or {}).get("due_on") or "")[:10]


def is_overdue(topic, today):
    """An open issue past its due date ("YYYY-MM-DD" strings)."""
    return topic.review == "open" and bool(getattr(topic, "due_on", "")) and topic.due_on < today


# --------------------------------------------------------------- unread

SEEN_FILE = os.path.join(os.environ.get("APPDATA", tempfile.gettempdir()), "BIMPulse", "issues-seen.json")


def thread_id(topic):
    cloud = getattr(topic.source, "cloud", None) if topic.source else None
    if not cloud or not topic.issue_key:
        return None
    return "|".join([cloud["audit_id"], cloud["module"], cloud["consultant"], topic.issue_key])


class Seen(object):
    """When this Windows user last looked at each BIMPulse issue's conversation, and the newest
    activity by someone else on it: an issue is unread when the second is later."""

    def __init__(self, path=SEEN_FILE):
        self.path = path
        self.latest = {}  # thread id -> newest created_at by someone else
        try:
            with io.open(path, encoding="utf-8") as f:
                self.seen = json.load(f)
        except (IOError, OSError, ValueError):
            self.seen = {}

    def note_events(self, events, me, audit_id=None):
        """Takes in activity (any order); `me` is the signed-in email (my own activity is never unread)."""
        for e in events or []:
            if me and (e.get("author_name") or "").lower() == me.lower():
                continue
            tid = "|".join([e.get("audit_id") or audit_id or "", e.get("module") or "", e.get("consultant") or "",
                            e.get("issue_key") or ""])
            if (e.get("created_at") or "") > self.latest.get(tid, ""):
                self.latest[tid] = e.get("created_at") or ""

    def unread(self, topic):
        tid = thread_id(topic)
        return bool(tid) and self.latest.get(tid, "") > self.seen.get(tid, "")

    def mark(self, topic):
        tid = thread_id(topic)
        if not tid or not self.unread(topic):
            return
        self.seen[tid] = self.latest[tid]
        try:
            folder = os.path.dirname(self.path)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            with io.open(self.path, "w", encoding="utf-8") as f:
                f.write(_to_text(json.dumps(self.seen, ensure_ascii=False)))
        except (IOError, OSError):
            pass


# --------------------------------------------------------------- housekeeping

def clean_cache(keep=(), folder=CACHE_DIR, days=CACHE_DAYS, now=None):
    """Removes downloaded results not written for `days` (except the open ones, `keep`), and
    folders left empty. Returns how many files went. Never fails."""
    now = time.time() if now is None else now
    keep = set(os.path.normcase(os.path.abspath(k)) for k in keep or ())
    removed = 0
    if not os.path.isdir(folder):
        return 0
    for base, dirs, files in os.walk(folder, topdown=False):
        for name in files:
            path = os.path.join(base, name)
            try:
                if os.path.normcase(os.path.abspath(path)) in keep:
                    continue
                if now - os.path.getmtime(path) > days * 86400:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        if base != folder:
            try:
                if not os.listdir(base):
                    os.rmdir(base)
            except OSError:
                pass
    return removed


def installed_version(extension_dir):
    """The installed plugin's commit (pyRevit installs it as a git clone), or None (a copy)."""
    git = os.path.join(extension_dir, ".git")
    try:
        with io.open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head[4:].strip()
        path = os.path.join(git, *ref.split("/"))
        if os.path.exists(path):
            with io.open(path, encoding="utf-8") as f:
                return f.read().strip() or None
        with io.open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" ")
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except (IOError, OSError):
        pass
    return None


def latest_version(transport=None):
    """The newest published commit of the plugin (GitHub), or None when it can't be reached."""
    try:
        status, data = (transport or default_transport)(
            "GET", RELEASES, {"Accept": "application/vnd.github.sha", "User-Agent": "BIMPulse-Revit"}, None)
    except CloudError:
        return None
    text = _to_text(data or b"").strip()
    return text if status == 200 and re.match(r"^[0-9a-f]{40}$", text) else None


# --------------------------------------------------------------- offline

OUTBOX_FILE = os.path.join(_APPDATA, "BIMPulse", "outbox.json")


def offline(error):
    """True when a call failed because BIMPulse couldn't be reached (not a refusal)."""
    return isinstance(error, CloudError) and not isinstance(error, SignedOut) and error.status == 0


class Outbox(object):
    """Comments, statuses and assignments made while BIMPulse can't be reached, kept on this PC
    (for the account that made them) and sent in order when it can be reached again."""

    def __init__(self, path=OUTBOX_FILE):
        self.path = path
        self._lock = threading.Lock()
        try:
            with io.open(path, encoding="utf-8") as f:
                self.items = json.load(f)
        except (IOError, OSError, ValueError):
            self.items = []

    def _save(self):
        try:
            folder = os.path.dirname(self.path)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            with io.open(self.path, "w", encoding="utf-8") as f:
                f.write(_to_text(json.dumps(self.items, ensure_ascii=False)))
        except (IOError, OSError):
            pass

    def add(self, account, kind, audit_id, body, png=None):
        """kind: "event", "bulk" or "assign"; png: a picture to upload first (events only)."""
        with self._lock:
            self.items.append({"account": (account or "").lower(), "kind": kind, "audit_id": audit_id,
                               "body": body, "png": base64.b64encode(png).decode("ascii") if png else None,
                               "saved_at": time.time()})
            self._save()
            return self.count(account)

    def count(self, account):
        account = (account or "").lower()
        return sum(1 for i in self.items if i["account"] == account)

    def flush(self, session):
        """Sends this account's waiting changes in order. Stops while still offline. Returns
        (sent, refused): refused are dropped with BIMPulse's reason (e.g. no longer allowed)."""
        account = (session.email or "").lower()
        sent, refused = 0, []
        with self._lock:
            mine = [i for i in self.items if i["account"] == account]
        for item in mine:
            try:
                body = dict(item["body"])
                if item.get("png"):
                    body["attachment"] = session.attach(item["audit_id"], base64.b64decode(item["png"]))
                if item["kind"] == "event":
                    session.add_event(item["audit_id"], body)
                elif item["kind"] == "bulk":
                    session.bulk(item["audit_id"], body)
                else:
                    session.assign(item["audit_id"], body)
                sent += 1
            except CloudError as e:
                if offline(e) or isinstance(e, SignedOut):
                    break
                refused.append(str(e))
            with self._lock:
                self.items = [i for i in self.items if i is not item]
                self._save()
        return sent, refused
