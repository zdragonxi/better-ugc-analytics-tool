"""Optional single-user password for the dashboard (stdlib PBKDF2-SHA256).

Only needed when the dashboard is reachable from other machines. Sessions live in memory,
so restarting the app signs everyone out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

ITERATIONS = 600_000
SESSION_TTL = 30 * 24 * 3600
MAX_FAILS = 5              # per client address …
FAIL_WINDOW = 300          # … per 5 minutes


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, it, salt, want = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 base64.b64decode(salt), int(it))
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


class Sessions:
    def __init__(self):
        self._tokens: dict[str, float] = {}
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[tok] = time.time() + SESSION_TTL
        return tok

    def valid(self, tok: str | None) -> bool:
        if not tok:
            return False
        with self._lock:
            exp = self._tokens.get(tok)
            if exp is None:
                return False
            if exp < time.time():
                self._tokens.pop(tok, None)
                return False
            return True

    def revoke(self, tok: str | None) -> None:
        with self._lock:
            self._tokens.pop(tok or "", None)

    def throttled(self, addr: str) -> bool:
        """Reserve one login attempt for `addr`; True when the budget is used up."""
        now = time.time()
        with self._lock:
            recent = [t for t in self._fails.get(addr, []) if now - t < FAIL_WINDOW]
            if len(recent) >= MAX_FAILS:
                self._fails[addr] = recent
                return True
            recent.append(now)
            self._fails[addr] = recent
            if len(self._fails) > 10_000:          # bound memory against address floods
                self._fails = {a: v for a, v in self._fails.items() if v and now - v[-1] < FAIL_WINDOW}
            return False

    def succeeded(self, addr: str) -> None:
        with self._lock:
            self._fails.pop(addr, None)
