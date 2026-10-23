"""Reading sales and financials from Roblox into the local database.

The rules that keep the stored history complete:

* Each store has its own WATERMARK ("synced through"). A walk goes newest → oldest until it
  passes the watermark, and the watermark only moves after that store's walk COMPLETED.
  An interrupted walk saves its newest pages first, so a rule like "stop at the first page
  with nothing new" would stop at page 1 on the retry and strand the older half of the gap
  forever; with a watermark the retry simply walks the same gap again.
* Roblox caps transaction pagination (roughly 100 pages / 10k rows) and answers a deeper
  cursor with HTTP 400. That is NOT the end of history — it is history we cannot read.
  The walk reports it as truncated and the watermark is left where it was; the monthly CSV
  export is the way to close such a gap.
* One store failing never stops the others, and never advances anyone's watermark but the
  stores that actually completed.
* The new watermark is the walk's START time minus a 30-minute overlap, so sales that
  Roblox lists slightly late are still picked up.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from . import roblox, store

PAGE_LIMIT = 100
PAGE_SLEEP = 2.0        # seconds between pages of one store
STORE_SPACING = 3.0     # seconds between stores: back-to-back bursts draw 30-60 s 429 penalties
FIN_SPACING = 1.0       # seconds between groups when reading financials
OVERLAP = timedelta(minutes=30)


class WalkResult:
    def __init__(self, source):
        self.source = source
        self.new = 0
        self.pages = 0
        self.truncated = False
        self.error: str | None = None
        self.completed = False

    def as_dict(self):
        return {"source": self.source, "new": self.new, "pages": self.pages,
                "truncated": self.truncated, "error": self.error, "completed": self.completed}


def tx_url(st, user_id: int | None) -> str:
    if st.group_id is not None:
        return roblox.GROUP_TX_URL.format(id=st.group_id)
    if not user_id:
        raise ValueError("user id unknown")
    return roblox.USER_TX_URL.format(id=user_id)


def _has_rows(db, source: str) -> bool:
    return db.execute("SELECT 1 FROM sales WHERE source=? LIMIT 1", (source,)).fetchone() is not None


def walk_store(db, s, url: str, source: str, since: datetime, sleep=time.sleep,
               log=print) -> WalkResult:
    """Walk one store newest → oldest until a page reaches back past `since`."""
    res = WalkResult(source)
    cursor = None
    while True:
        params = {"transactionType": "Sale", "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        status, data = roblox.get_json(s, url, params=params, sleep=sleep, allow_400=bool(cursor))
        if status == 400:
            # pagination depth cap — rows fetched so far are saved; what lies beyond is unread
            res.truncated = True
            log(f"[sync] {source}: Roblox stopped paginating at page {res.pages + 1}; older "
                f"history was NOT read — import the monthly CSV export to fill it")
            return res
        rows = (data or {}).get("data") or []
        if not rows:
            # An empty-but-200 page is only believable as "no sales at all": on page 1 of a
            # store with nothing stored, or never. Sales history does not shrink, so an empty
            # page anywhere else is an unknown answer — keep the watermark and retry later.
            if cursor is None and not _has_rows(db, source):
                res.completed = True
            else:
                res.error = "Roblox returned an empty page mid-history — not trusted, will retry"
                log(f"[sync] {source}: {res.error}")
            return res
        page_new = sum(store.upsert_api_tx(db, tx, source) for tx in rows)
        db.commit()
        res.new += page_new
        res.pages += 1
        oldest = store.parse_ts(rows[-1].get("created", ""))
        if oldest is not None and oldest < since:
            res.completed = True
            return res
        cursor = (data or {}).get("nextPageCursor")
        if not cursor:
            res.completed = True
            return res
        sleep(PAGE_SLEEP)


def sync_stores(db, s, cfg, user_id: int | None, now: datetime | None = None,
                sleep=time.sleep, log=print) -> list[WalkResult]:
    """Walk every configured store once. Watermarks advance per store, only on completion."""
    results = []
    for i, st in enumerate(cfg.stores):
        if i:
            sleep(STORE_SPACING)
        res = WalkResult(st.source)
        walk_start = now or datetime.now(timezone.utc)
        since = store.get_watermark(db, st.source) or \
            (walk_start - timedelta(days=cfg.initial_sync_days))
        try:
            res = walk_store(db, s, tx_url(st, user_id), st.source, since, sleep=sleep, log=log)
        except roblox.AuthError:
            raise                       # the cookie is dead for every store — stop the round
        except roblox.ReadOnlyViolation:
            raise
        except Exception as e:          # noqa: BLE001 — isolate this store, keep going
            res.error = roblox.safe_error(e)
            log(f"[sync] {st.source}: {res.error}")
        if res.completed and not res.truncated and not res.error:
            store.set_watermark(db, st.source, walk_start - OVERLAP)
        results.append(res)
    return results


# ── financials ───────────────────────────────────────────────────────────────

def fetch_group_financials(s, gid: int, sleep=time.sleep) -> dict:
    """{funds, pending, sales_month} for one group. A key whose call failed is OMITTED —
    an unreadable number is unknown, not zero."""
    out = {}
    try:
        _, j = roblox.get_json(s, roblox.GROUP_FUNDS_URL.format(id=gid), attempts=2, sleep=sleep)
        if isinstance(j, dict) and isinstance(j.get("robux"), (int, float)):
            out["funds"] = int(j["robux"])
    except roblox.AuthError:
        raise
    except Exception:               # noqa: BLE001
        pass
    try:
        _, j = roblox.get_json(s, roblox.GROUP_SUMMARY_URL.format(id=gid), attempts=2, sleep=sleep)
        if isinstance(j, dict):
            if isinstance(j.get("pendingRobux"), (int, float)):
                out["pending"] = int(j["pendingRobux"])
            if isinstance(j.get("itemSaleRobux"), (int, float)):
                out["sales_month"] = int(j["itemSaleRobux"])
    except roblox.AuthError:
        raise
    except Exception:               # noqa: BLE001
        pass
    return out


def merge_financials(prev: dict, fresh: dict, now: float | None = None) -> dict:
    """Layer a fresh (possibly partial) fetch over the last-known numbers. Missing keys
    keep their previous value, so a throttled reply never blanks a card."""
    merged = dict(prev or {})
    if fresh:
        merged.update(fresh)
        merged["updated"] = int(now if now is not None else time.time())
    return merged


def refresh_financials(db, s, cfg, sleep=time.sleep) -> dict:
    """Fetch every group's financials and persist them merged over the last snapshot."""
    snap = store.get_json_meta(db, "financials", {}) or {}
    for i, st in enumerate(cfg.groups):
        if i:
            sleep(FIN_SPACING)
        fresh = fetch_group_financials(s, st.group_id, sleep=sleep)
        snap[st.source] = merge_financials(snap.get(st.source), fresh)
    store.set_meta(db, "financials", json.dumps(snap))
    return snap


def fetch_balance(s, sleep=time.sleep) -> int | None:
    """The cookie account's spendable Robux, or None if it could not be read."""
    try:
        _, j = roblox.get_json(s, roblox.USER_BALANCE_URL, attempts=2, sleep=sleep)
    except roblox.AuthError:
        raise
    except Exception:               # noqa: BLE001
        return None
    if isinstance(j, dict) and isinstance(j.get("robux"), (int, float)):
        return int(j["robux"])
    return None
