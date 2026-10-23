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

import re
import time
from urllib.parse import urlsplit

import requests

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
USER_TX_URL = "https://economy.roblox.com/v2/users/{id}/transactions"
GROUP_TX_URL = "https://economy.roblox.com/v2/groups/{id}/transactions"
GROUP_FUNDS_URL = "https://economy.roblox.com/v1/groups/{id}/currency"
GROUP_SUMMARY_URL = "https://economy.roblox.com/v1/groups/{id}/revenue/summary/Month"
USER_BALANCE_URL = "https://economy.roblox.com/v1/user/currency"
ITEMS_BY_CREATOR_URL = "https://itemconfiguration.roblox.com/v1/items/by-creator"

ALLOWED_SUFFIX = ".roblox.com"
TIMEOUT = 30


class ReadOnlyViolation(RuntimeError):
    """Raised before anything is sent when code tries a non-GET or non-Roblox request."""


class AuthError(RuntimeError):
    """Roblox answered 401: the cookie is missing, expired or was logged out elsewhere."""


class RobloxUnavailable(RuntimeError):
    """Repeated 429 / 5xx / transport errors: an UNKNOWN answer, never an empty one."""


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
             sleep=time.sleep, allow_400: bool = False):
    """GET with the back-off the economy API needs. Returns (status, json_or_None).

    429 → wait 30 s × attempt (economy endpoints share one per-cookie budget, and a burst
    earns a 30–60 s penalty). 5xx and transport errors → 10 s × attempt. 401 → AuthError.
    When every attempt fails the result is UNKNOWN, so this raises RobloxUnavailable rather
    than returning something a caller could mistake for "no data"."""
    last = None
    for attempt in range(attempts):
        try:
            r = s.get(url, params=params, timeout=TIMEOUT)
        except ReadOnlyViolation:
            raise
        except requests.RequestException as e:
            last = f"{type(e).__name__}"
            sleep(10 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise AuthError("Roblox rejected the cookie (401) — it expired or was logged out; "
                            "paste a fresh one")
        if r.status_code == 429:
            last = "429 rate limited"
            sleep(30 * (attempt + 1))
            continue
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            sleep(10 * (attempt + 1))
            continue
        if r.status_code == 400 and allow_400:
            return 400, None
        if not r.ok:
            raise RobloxUnavailable(f"HTTP {r.status_code} from {urlsplit(url).path}")
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
