"""The only code that talks to Roblox — and it can only READ.

`ReadOnlySession` refuses, at the transport layer, anything that is not an HTTPS GET to a
*.roblox.com host. Every request this app makes goes through `send()`, including redirects,
so the guard cannot be bypassed by a redirect or by a future caller using `.post()`.

Why read-only matters here: your .ROBLOSECURITY cookie is a full login. Several Roblox
endpoints change account state, and some of them invalidate the session (log you out
everywhere) when called with the right headers. This tool needs none of them, so it is
built so that it cannot call them.
"""
from __future__ import annotations

import collections
import re
import threading
import time
from urllib.parse import urlsplit

import requests

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
USER_TX_URL = "https://economy.roblox.com/v2/users/{id}/transactions"
GROUP_TX_URL = "https://economy.roblox.com/v2/groups/{id}/transactions"
# the group sales ledger the Creator Hub reads: time-windowed, with location and hold status
GROUP_TXREC_URL = "https://apis.roblox.com/transaction-records-api/v2/groups/{id}/transactions"
GROUP_FUNDS_URL = "https://economy.roblox.com/v1/groups/{id}/currency"
GROUP_SUMMARY_URL = "https://economy.roblox.com/v1/groups/{id}/revenue/summary/Month"
USER_BALANCE_URL = "https://economy.roblox.com/v1/user/currency"
ITEMS_BY_CREATOR_URL = "https://itemconfiguration.roblox.com/v1/items/by-creator"

ALLOWED_SUFFIX = ".roblox.com"
TIMEOUT = 30
# sent with transaction-records requests only, as the Creator Hub page itself does
TXREC_HEADERS = {"Origin": "https://create.roblox.com", "Referer": "https://create.roblox.com/"}


class ReadOnlyViolation(RuntimeError):
    """Raised before anything is sent when code tries a non-GET or non-Roblox request."""


class AuthError(RuntimeError):
    """Roblox answered 401: the cookie is missing, expired or was logged out elsewhere."""


class RobloxUnavailable(RuntimeError):
    """Repeated 429 / 5xx / transport errors: an UNKNOWN answer, never an empty one."""


class HttpError(RobloxUnavailable):
    """A definite HTTP answer other than 401 / 429 / 5xx (for example 403 or 404)."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


_CRED_RE = re.compile(r"(//)[^/@\s:]+:[^/@\s]+@")


def safe_error(e: BaseException, limit: int = 200) -> str:
    """An exception as text with any user:password@ (e.g. inside a proxy URL) removed, so
    error messages can be logged and shown on the dashboard."""
    return _CRED_RE.sub(lambda m: m.group(1) + "***@", f"{type(e).__name__}: {e}")[:limit]


def check_request(method: str, url: str) -> None:
    if (method or "").upper() != "GET":
        raise ReadOnlyViolation(f"refusing {method} — this tool only sends GET requests")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == ALLOWED_SUFFIX[1:] or host.endswith(ALLOWED_SUFFIX)):
        raise ReadOnlyViolation(f"refusing a request to {parts.scheme}://{host} — "
                                f"only https://*.roblox.com is allowed")


class ReadOnlySession(requests.Session):
    """A requests.Session that can only issue HTTPS GETs to *.roblox.com."""

    def request(self, method, url, *args, **kwargs):
        check_request(method, url)
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(method, url, *args, **kwargs)

    def send(self, request, **kwargs):
        # also covers every redirect hop, which requests sends without going through request()
        check_request(request.method, request.url)
        return super().send(request, **kwargs)

    def __repr__(self):            # never show cookies or proxy credentials
        return "<ReadOnlySession>"


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _header_num(v) -> float | None:
    """The first number in a header value ('49', '50, 50;w=60' → 50), or None."""
    m = _NUM_RE.search(str(v)) if v is not None else None
    return float(m.group(0)) if m else None


class RateLimiter:
    """Paces requests that draw on one Roblox rate budget. Thread-safe.

    Two brakes. Our own cap: at most `per_minute` requests in any `window` seconds, set below
    Roblox's published limit so the same account still has room in a browser. And Roblox's own
    answer: once `x-ratelimit-remaining` is down to `reserve`, every caller waits out
    `x-ratelimit-reset`, and a 429 pauses every caller for its `retry-after`. Because the
    server's headers are obeyed, two processes on one account (the dashboard and a `backfill`)
    still stay inside the shared budget."""

    MAX_PAUSE = 300.0           # never believe a header asking for more than this

    def __init__(self, per_minute: int = 40, window: float = 60.0, reserve: int = 2,
                 clock=time.monotonic, sleep=time.sleep):
        self.per_minute = per_minute
        self.window = window
        self.reserve = reserve
        self._clock = clock
        self._sleep = sleep
        self._sent = collections.deque()
        self._until = 0.0
        self._lock = threading.Lock()
        self.waited = 0.0       # total seconds spent waiting (for tests and logs)

    def acquire(self) -> None:
        """Block until one more request may be sent, and count it."""
        while True:
            with self._lock:
                now = self._clock()
                while self._sent and now - self._sent[0] >= self.window:
                    self._sent.popleft()
                wait = self._until - now
                if len(self._sent) >= self.per_minute:
                    wait = max(wait, self._sent[0] + self.window - now)
                if wait <= 0:
                    self._sent.append(now)
                    return
            self.waited += wait
            self._sleep(wait)

    def observe(self, status: int, headers) -> None:
        """Feed back one answer's status and rate-limit headers."""
        h = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        pause = None
        if status == 429:
            pause = _header_num(h.get("retry-after"))
            if pause is None:
                pause = _header_num(h.get("x-ratelimit-reset"))
            if pause is None:
                pause = self.window
        else:
            left = _header_num(h.get("x-ratelimit-remaining"))
            if left is not None and left <= self.reserve:
                pause = _header_num(h.get("x-ratelimit-reset"))
                if pause is None:
                    pause = self.window
        if pause is None:
            return
        with self._lock:
            # +0.5 s: the server's window edge and our clock are not the same clock
            self._until = max(self._until, self._clock() + min(pause, self.MAX_PAUSE) + 0.5)


def make_session(cookie: str, proxy: str = "") -> ReadOnlySession:
    s = ReadOnlySession()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.roblox.com/transactions",
    })
    if cookie:
        # domain-scoped: requests only ever attaches it to *.roblox.com
        s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def get_json(s, url: str, params: dict | None = None, attempts: int = 5,
             sleep=time.sleep, allow_400: bool = False, limiter: RateLimiter | None = None,
             headers: dict | None = None):
    """GET with the back-off the economy API needs. Returns (status, json_or_None).

    429 → wait 30 s × attempt (economy endpoints share one per-cookie budget, and a burst
    earns a 30–60 s penalty) — or, with a `limiter`, as long as the 429's retry-after says.
    5xx and transport errors → 10 s × attempt. 401 → AuthError. Any other non-2xx raises
    HttpError carrying the status. When every attempt fails the result is UNKNOWN, so this
    raises RobloxUnavailable rather than returning something a caller could mistake for
    "no data"."""
    last = None
    for attempt in range(attempts):
        if limiter is not None:
            limiter.acquire()
        try:
            r = s.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except ReadOnlyViolation:
            raise
        except requests.RequestException as e:
            last = f"{type(e).__name__}"
            sleep(10 * (attempt + 1))
            continue
        if limiter is not None:
            limiter.observe(r.status_code, r.headers)
        if r.status_code == 401:
            raise AuthError("Roblox rejected the cookie (401) — it expired or was logged out; "
                            "paste a fresh one")
        if r.status_code == 429:
            last = "429 rate limited"
            if limiter is None:
                sleep(30 * (attempt + 1))
            continue
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            sleep(10 * (attempt + 1))
            continue
        if r.status_code == 400 and allow_400:
            return 400, None
        if not r.ok:
            raise HttpError(r.status_code, f"HTTP {r.status_code} from {urlsplit(url).path}")
        try:
            return r.status_code, r.json()
        except ValueError:
            raise RobloxUnavailable(f"non-JSON answer from {urlsplit(url).path}") from None
    raise RobloxUnavailable(f"gave up after {attempts} attempts ({last})")


def authenticated_user(s) -> dict:
    """{"id": int, "name": str} of the cookie's account."""
    _, j = get_json(s, AUTH_URL, attempts=3)
    if not isinstance(j, dict) or not j.get("id"):
        raise RobloxUnavailable("could not read the authenticated user")
    return {"id": int(j["id"]), "name": str(j.get("name") or "")}
