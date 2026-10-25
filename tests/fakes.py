"""A fake Roblox: serves canned JSON for the endpoints this app reads, at the HTTP adapter
layer, so requests travel through the REAL ReadOnlySession guard on their way."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter


class FakeRoblox:
    def __init__(self):
        self.calls = []            # (method, url, headers)
        self.routes = {}           # path -> callable(params) -> (status, body)

    def route(self, path, fn):
        self.routes[path] = fn

    def json(self, path, body, status=200):
        self.routes[path] = lambda params: (status, body)

    def handle(self, prep):
        self.calls.append((prep.method, prep.url, dict(prep.headers)))
        u = urlsplit(prep.url)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        fn = self.routes.get(u.path)
        # a route answers (status, body) or (status, body, headers)
        status, body, *extra = fn(params) if fn else (404, {"errors": [{"message": "not found"}]})
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode() if body is not None else b""
        r.headers["Content-Type"] = "application/json"
        r.headers.update(extra[0] if extra else {})
        r.url = prep.url
        r.request = prep
        r.encoding = "utf-8"
        return r

    def __enter__(self):
        self._orig = HTTPAdapter.send
        fake = self

        def send(adapter, prep, **kw):
            return fake.handle(prep)
        HTTPAdapter.send = send
        return self

    def __exit__(self, *exc):
        HTTPAdapter.send = self._orig


def pages(txs, size=100, cap_pages=None):
    """A transactions route serving `txs` (newest first) in cursor pages. With cap_pages,
    any cursor deeper than that answers 400, like Roblox's pagination depth cap."""
    def fn(params):
        start = int(params.get("cursor") or 0)
        if cap_pages is not None and start and start // size >= cap_pages:
            return 400, {"errors": [{"message": "bad cursor"}]}
        chunk = txs[start:start + size]
        nxt = str(start + size) if start + size < len(txs) else None
        return 200, {"data": chunk, "nextPageCursor": nxt, "previousPageCursor": None}
    return fn


def txrec_path(gid):
    return f"/transaction-records-api/v2/groups/{gid}/transactions"


def txrec_pages(rows, size=100, require_reason=True):
    """A transaction-records route over `rows`: answers a time window
    (startTimeMillis..endTimeMillis, both inclusive) newest first, in cursor pages, in the
    recorded response shape. Without ledgerReason it answers 400, as Roblox does."""
    from roblox_sales import store

    def ms(r):
        return int(store.parse_ts(r["createdTime"]).timestamp() * 1000)

    def fn(params):
        if require_reason and params.get("ledgerReason") != "SaleOfGood":
            return 400, {"errors": [{"message": "Missing Request Parameter"}]}
        lo, hi = int(params["startTimeMillis"]), int(params["endTimeMillis"])
        win = sorted((r for r in rows if lo <= ms(r) <= hi), key=ms, reverse=True)
        start = int(params.get("cursor") or 0)
        chunk = win[start:start + size]
        more = start + size < len(win)
        return 200, {"transactions": chunk, "nextCursor": str(start + size) if more else None,
                     "previousCursor": None, "hasMore": more}
    return fn
