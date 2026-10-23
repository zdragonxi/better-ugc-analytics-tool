"""The dashboard's HTTP server (stdlib only).

Routes:
  GET  /                  the dashboard page
  GET  /static/<file>     its CSS / JS (a fixed allowlist)
  GET  /data              JSON for the page — sales rows sent INCREMENTALLY (see data())
  POST /import-csv        one Creator Hub monthly export (raw body, filename in X-Filename)
  POST /refresh-catalog   ask the poller to recount items on sale (at most once a minute)
  GET  /login, POST /login, POST /sign-out   only when a password is configured

Binds to 127.0.0.1 by default. A Host-header allowlist stops other websites from reaching a
loopback dashboard through DNS rebinding, and state-changing requests need a custom header
that a cross-site form cannot send.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import auth, store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "golden.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "chart-4.4.1.umd.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
MAX_UPLOAD = 64 * 1024 * 1024
SESSION_COOKIE = "rsa_session"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")

_ROW_COLS = "rowid, created, robux, source, asset_name, location"


def read_data(db, cfg, poller_state: dict, epoch: str = "", have_rowid: int = -1,
              have_count: int = -1) -> dict:
    """Everything the page needs. Sale rows are incremental: a client that already holds
    the first `have_count` rows (up to rowid `have_rowid`) of the current `epoch` gets only
    `rows_add`; anyone else gets the full `rows`. The epoch changes on every CSV import,
    which deletes and rewrites rows, so no client can keep a stale copy across one."""
    out = {}
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN")                    # one snapshot for the key and the rows
    try:
        cur_epoch = store.rows_epoch(db)
        total, max_rowid = db.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM sales").fetchone()
        base_ok = (epoch == cur_epoch and have_rowid >= 0 and have_count >= 0 and
                   db.execute("SELECT COUNT(*) FROM sales WHERE rowid <= ?",
                              (have_rowid,)).fetchone()[0] == have_count)
        if base_ok:
            out["rows_add"] = [list(r) for r in db.execute(
                f"SELECT {_ROW_COLS} FROM sales WHERE rowid > ? ORDER BY rowid", (have_rowid,))]
        else:
            out["rows"] = [list(r) for r in db.execute(f"SELECT {_ROW_COLS} FROM sales ORDER BY rowid")]
        fin = store.get_json_meta(db, "financials", {}) or {}
        bal = store.get_balance(db)
        imports = store.imports_list(db)
        cat = store.get_json_meta(db, "catalog", {}) or {}
    finally:
        db.commit()
    names = {s.source: s.name for s in cfg.stores}
    out.update({
        "epoch": cur_epoch, "max_rowid": max_rowid, "count": total,
        "stores": [{"source": s.source, "name": s.name, "combine": s.combine,
                    "group": s.group_id is not None} for s in cfg.stores],
        "financials": {k: v for k, v in fin.items() if k in names},
        "balance": bal,
        "catalog": {k: {f: v.get(f) for f in ("items", "as_of", "attempt", "error")}
                    for k, v in cat.items() if k in names},
        "imports": [dict(i, name=names.get(i["source"], i["source"])) for i in imports],
        "status": {k: poller_state.get(k) for k in
                   ("polling", "last_sync", "sync_error", "signed_in")},
        "server_time": int(time.time()),
    })
    return out


class App:
    """Shared state for the request handlers."""

    def __init__(self, cfg, poller=None):
        self.cfg = cfg
        self.poller = poller
        self.sessions = auth.Sessions()
        port = cfg.port
        self.allowed_hosts = set(cfg.allowed_hosts)
        if cfg.is_loopback:
            self.allowed_hosts |= {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
            if port == 80:
                self.allowed_hosts |= {"127.0.0.1", "localhost", "[::1]"}

    @property
    def poller_state(self):
        return self.poller.state if self.poller else {"polling": False}


def make_handler(app: App):
    cfg = app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "RobloxSalesAnalytics"
        sys_version = ""

        def log_message(self, fmt, *args):      # no per-request noise, no URLs in logs
            pass

        # ── plumbing ───────────────────────────────────────────────────────────
        def _send(self, status, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, obj, extra=None):
            self._send(status, json.dumps(obj, separators=(",", ":")).encode(),
                       "application/json", extra)

        def _host_ok(self) -> bool:
            if not app.allowed_hosts:          # non-loopback bind with no allowlist: the
                return True                    # password is the protection
            return (self.headers.get("Host") or "").strip().lower() in app.allowed_hosts

        def _cookie_token(self):
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:                   # noqa: BLE001
                return None
            m = c.get(SESSION_COOKIE)
            return m.value if m else None

        def _authed(self) -> bool:
            return not cfg.password_hash or app.sessions.valid(self._cookie_token())

        def _same_origin_write(self) -> bool:
            """A state-changing request must carry our custom header (a cross-site form
            cannot add one without a CORS preflight, which this server never grants) and,
            when the browser sends an Origin, it must be this server."""
            if self.headers.get("X-RSA-Request") != "1":
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = urlsplit(origin).netloc.lower()
                if host != (self.headers.get("Host") or "").lower():
                    return False
            return True

        def _static(self, name):
            ctype = STATIC_FILES.get(name)
            path = os.path.join(STATIC_DIR, name)
            if not ctype or not os.path.isfile(path):
                return self._send(404, b"not found", "text/plain")
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # ── GET ────────────────────────────────────────────────────────────────
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._send(421, b"unknown host", "text/plain")
            url = urlsplit(self.path)
            path = url.path
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/login":
                if not cfg.password_hash:
                    return self._redirect("/")
                return self._login_page(error=parse_qs(url.query).get("e", [""])[0] == "1")
            if not self._authed():
                if path == "/data":
                    return self._json(401, {"error": "sign in again"})
                return self._redirect("/login")
            if path == "/" or path == "/index.html":
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/data":
                q = parse_qs(url.query)
                one = lambda k, d="": (q.get(k) or [d])[0]
                try:
                    have_rowid, have_count = int(one("rowid", "-1")), int(one("count", "-1"))
                except ValueError:
                    have_rowid = have_count = -1
                if one("live") == "1" and app.poller:
                    app.poller.refresh_now()
                db = store.connect(cfg.db_path)
                try:
                    data = read_data(db, cfg, app.poller_state, one("epoch"), have_rowid, have_count)
                except sqlite3.Error as e:
                    # deliberately no "rows" key: the page keeps what it already shows
                    return self._json(200, {"status": {"sync_error": f"database: {e}"},
                                            "error": True})
                finally:
                    db.close()
                return self._json(200, data)
            return self._send(404, b"not found", "text/plain")

        # ── POST ───────────────────────────────────────────────────────────────
        def do_POST(self):
            if not self._host_ok():
                return self._send(421, b"unknown host", "text/plain")
            path = urlsplit(self.path).path
            if path == "/login" and cfg.password_hash:
                return self._login()
            if path == "/sign-out":
                app.sessions.revoke(self._cookie_token())
                return self._redirect("/login", clear=True)
            if not self._authed():
                return self._json(401, {"error": "sign in again"})
            if not self._same_origin_write():
                return self._json(403, {"error": "cross-site request refused"})
            if path == "/import-csv":
                return self._import_csv()
            if path == "/refresh-catalog":
                if not app.poller or not app.poller.state.get("polling"):
                    return self._json(409, {"ok": False, "error": "not polling Roblox"})
                app.poller.refresh_catalog_now()
                return self._json(202, {"ok": True})
            return self._json(404, {"error": "not found"})

        def _import_csv(self):
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_UPLOAD:
                return self._json(413, {"ok": False, "error": "empty or too large (max 64 MB)"})
            fname = os.path.basename(unquote(self.headers.get("X-Filename") or ""))
            known = [s.group_id for s in cfg.groups]
            try:
                store.check_csv_name(fname, known)      # refuse before writing anything
            except store.ImportRefused as e:
                self.rfile.read(length)
                return self._json(200, {"ok": False, "filename": fname, "error": str(e)})
            tmpdir = os.path.join(tempfile.gettempdir(), "rsa-import-" + uuid.uuid4().hex)
            os.makedirs(tmpdir, mode=0o700)
            dest = os.path.join(tmpdir, fname)          # the importer reads store+month from it
            try:
                with open(dest, "wb") as out:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                db = store.connect(cfg.db_path)
                try:
                    st = cfg.group_store(int(store.CSV_NAME_RE.match(fname).group(1)))
                    res = store.import_csv(db, dest, known, display_name=st.name if st else None)
                    res["ok"] = True
                    res["imports"] = [dict(i, name=(cfg.store(i["source"]).name
                                                    if cfg.store(i["source"]) else i["source"]))
                                      for i in store.imports_list(db)]
                finally:
                    db.close()
            except store.ImportRefused as e:
                res = {"ok": False, "filename": fname, "error": str(e)}
            except Exception as e:                      # noqa: BLE001
                res = {"ok": False, "filename": fname, "error": f"{type(e).__name__}: {e}"[:200]}
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return self._json(200, res)

        # ── login ──────────────────────────────────────────────────────────────
        def _redirect(self, to, cookie: str | None = None, clear=False):
            extra = {"Location": to}
            if cookie:
                extra["Set-Cookie"] = (f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; "
                                       f"SameSite=Strict; Max-Age={auth.SESSION_TTL}")
            elif clear:
                extra["Set-Cookie"] = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            self._send(303, b"", "text/plain", extra)

        def _login_page(self, error=False):
            msg = '<p class="err" role="alert">Wrong password, or too many attempts — wait a few minutes.</p>' \
                if error else ""
            html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>Sign in · Sales Analytics</title>'
                    f'<link rel="stylesheet" href="/static/style.css"></head>'
                    f'<body class="login"><form method="post" action="/login" class="panel">'
                    f'<h1>Sales Analytics</h1>{msg}<label for="pw">Password</label>'
                    f'<input id="pw" name="password" type="password" autocomplete="current-password" '
                    f'required autofocus><button type="submit">Sign in</button></form></body></html>')
            self._send(200, html.encode(), "text/html; charset=utf-8")

        def _login(self):
            addr = self.client_address[0]
            try:
                length = min(int(self.headers.get("Content-Length") or "0"), 4096)
            except ValueError:
                length = 0
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            pw = (form.get("password") or [""])[0]
            if app.sessions.throttled(addr) or not auth.verify_password(pw, cfg.password_hash):
                return self._redirect("/login?e=1")
            app.sessions.succeeded(addr)
            return self._redirect("/", cookie=app.sessions.create())

    return Handler


def serve(cfg, poller=None):
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), BaseHTTPRequestHandler)
    cfg.port = httpd.server_address[1]        # the real port when 0 (tests) was asked for
    httpd.RequestHandlerClass = make_handler(App(cfg, poller))
    httpd.daemon_threads = True
    return httpd
