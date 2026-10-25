"""Reading sales and financials from Roblox into the local database.

Groups are read from the transaction-records API (the ledger the Creator Hub uses): it is
asked for TIME WINDOWS, reports where each sale happened (in-game / website) and whether it is
still held, and keeps months of history. The account's own store, and any group for which that
API stops working, is read from the older economy transactions API instead.

The rules that keep the stored history complete:

* Each store has its own WATERMARK ("synced through"), and it only moves after the stretch
  of history before it was read to the end. An interrupted read is simply read again.
* Transaction-records: a sync reads the window from the watermark (minus a 10-minute
  overlap) to one minute ago, following `nextCursor` until `hasMore` is false. Long windows
  are read one day at a time, oldest first, and the watermark moves after each completed day,
  so a long catch-up that is interrupted keeps the days it finished.
* Economy API: a walk goes newest → oldest until it passes the watermark. Roblox caps that
  pagination (roughly 100 pages / 10k rows) and answers a deeper cursor with HTTP 400. That is
  NOT the end of history — it is history we cannot read. The walk reports it as truncated and
  the watermark is left where it was; the monthly CSV export is the way to close such a gap.
* An empty answer is believed only where nothing is stored: an empty window that already
  holds stored sales, or an empty economy page mid-history, is an unknown answer — the
  watermark stays and the read is retried.
* One store failing never stops the others, and never advances anyone's watermark but the
  stores that actually completed.
* Every transaction-records request goes through one shared RateLimiter (Roblox allows 50
  requests a minute per account, shared by all groups; this app uses at most 40).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from . import roblox, store

PAGE_LIMIT = 100
PAGE_SLEEP = 2.0        # seconds between pages of one store (economy API)
STORE_SPACING = 3.0     # seconds between stores: back-to-back bursts draw 30-60 s 429 penalties
FIN_SPACING = 1.0       # seconds between groups when reading financials
OVERLAP = timedelta(minutes=30)          # economy API: new watermark = walk start - this

# transaction-records API
TXREC_OVERLAP = timedelta(minutes=10)    # each sync re-reads this much before the watermark
TXREC_END_LAG = timedelta(seconds=60)    # windows end this long before now
SLICE = timedelta(days=1)                # long windows are read a day at a time, oldest first
SLICE_OVERLAP = timedelta(seconds=1)     # adjacent days share a second (window ends may be
                                         # exclusive); the repeated rows are deduplicated
MAX_WINDOW_PAGES = 2000                  # a runaway cursor chain in one day stops here
EMPTY_EDGE = timedelta(seconds=store.DUPE_WINDOW_S)   # see walk_window's empty-answer check
UNUSABLE_STATUS = {400, 403, 404, 405, 410}
FALLBACK_AFTER = 3                       # consecutive unusable answers before falling back
TXREC_RETRY_S = 6 * 3600                 # while on the fallback, retry the new API this often
POLL_BUDGET = 60                         # requests per store per background round (catch-up
                                         # continues next round; `sync` on the CLI has no cap)
BACKFILL_SINCE = datetime(2026, 6, 16, tzinfo=timezone.utc)   # the API's first data, as seen
                                                              # in October 2026
LIMITER = roblox.RateLimiter(per_minute=40)


class Unusable(RuntimeError):
    """The transaction-records API answered, definitely, that it cannot serve this store
    (HTTP 400/403/404) or in a shape this code does not understand."""


class WalkResult:
    def __init__(self, source, api="economy"):
        self.source = source
        self.api = api                  # 'txrec' or 'economy'
        self.fallback: str | None = None    # why a group is on the economy API, if it is
        self.new = 0
        self.pages = 0
        self.requests = 0
        self.loc_changed = 0
        self.behind = False             # stopped at the round's request budget; not an error
        self.truncated = False
        self.error: str | None = None
        self.completed = False

    def as_dict(self):
        return {"source": self.source, "api": self.api, "fallback": self.fallback,
                "new": self.new, "pages": self.pages, "behind": self.behind,
                "truncated": self.truncated, "error": self.error, "completed": self.completed}


def tx_url(st, user_id: int | None) -> str:
    if st.group_id is not None:
        return roblox.GROUP_TX_URL.format(id=st.group_id)
    if not user_id:
        raise ValueError("user id unknown")
    return roblox.USER_TX_URL.format(id=user_id)


def _has_rows(db, source: str) -> bool:
    return db.execute("SELECT 1 FROM sales WHERE source=? LIMIT 1", (source,)).fetchone() is not None


# ── economy API (the account's own store, and the fallback for groups) ─────────

def walk_store(db, s, url: str, source: str, since: datetime, sleep=time.sleep,
               log=print, res: WalkResult | None = None) -> WalkResult:
    """Walk one store newest → oldest until a page reaches back past `since`."""
    res = res or WalkResult(source)
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


def _sync_economy(db, s, st, url: str, cfg, now: datetime, res: WalkResult, sleep, log):
    since = store.get_watermark(db, st.source) or (now - timedelta(days=cfg.initial_sync_days))
    try:
        walk_store(db, s, url, st.source, since, sleep=sleep, log=log, res=res)
    except (roblox.AuthError, roblox.ReadOnlyViolation):
        raise                           # a dead cookie stops the round; a violation is a bug
    except Exception as e:              # noqa: BLE001 — isolate this store, keep going
        res.error = roblox.safe_error(e)
        log(f"[sync] {st.source}: {res.error}")
    if res.completed and not res.truncated and not res.error:
        store.set_watermark(db, st.source, now - OVERLAP)
    return res


# ── transaction-records API (groups) ──────────────────────────────────────────

def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def walk_window(db, s, st, start: datetime, end: datetime, res: WalkResult, limiter,
                sleep=time.sleep, log=print) -> bool:
    """Read every sale of group store `st` created in [start, end], following nextCursor
    until hasMore is false. True when the window was read to the end; False (with res.error
    set) when it must be read again. Raises Unusable for an answer that says this API cannot
    serve the store; AuthError and RobloxUnavailable pass through."""
    url = roblox.GROUP_TXREC_URL.format(id=st.group_id)
    cursor = None
    pages = 0
    while True:
        params = {"ledgerReason": "SaleOfGood", "limit": PAGE_LIMIT,
                  "startTimeMillis": _ms(start), "endTimeMillis": _ms(end)}
        if cursor:
            params["cursor"] = cursor
        res.requests += 1
        try:
            _, data = roblox.get_json(s, url, params=params, sleep=sleep, limiter=limiter,
                                      headers=roblox.TXREC_HEADERS)
        except roblox.HttpError as e:
            if e.status in UNUSABLE_STATUS:
                raise Unusable(f"HTTP {e.status}") from None
            raise
        if not isinstance(data, dict) or not isinstance(data.get("transactions"), list) \
                or not isinstance(data.get("hasMore"), bool):
            raise Unusable("unexpected answer shape")
        try:
            rows = [store.parse_txrec(t) for t in data["transactions"]]
        except store.SchemaError as e:
            raise Unusable(str(e)) from None
        if not rows:
            if data["hasMore"]:
                res.error = "Roblox returned an empty page that says more follow — not trusted"
                log(f"[sync] {st.source}: {res.error}")
                return False
            if cursor is None and end - start > 2 * EMPTY_EDGE:
                # Sales history does not shrink: "no sales here" is believable only where
                # nothing is stored. Otherwise it is an unknown answer, read again later.
                # Rows near the edges do not count: one stored from the economy API or a
                # CSV can sit a few seconds inside a window whose API twin lies outside it.
                held = store.count_in_range(db, st.source, start + EMPTY_EDGE, end - EMPTY_EDGE)
                if held:
                    res.error = (f"Roblox listed no sales for {start:%Y-%m-%d %H:%M} – "
                                 f"{end:%Y-%m-%d %H:%M} UTC, where {held:,} are stored — "
                                 f"not trusted, will retry")
                    log(f"[sync] {st.source}: {res.error}")
                    return False
            return True
        for r in rows:
            new, moved = store.upsert_txrec(db, r, st.source)
            res.new += new
            res.loc_changed += moved
        db.commit()
        res.pages += 1
        pages += 1
        if not data["hasMore"]:
            return True
        cursor = data.get("nextCursor")
        if not cursor:
            raise Unusable("hasMore without a nextCursor")
        if pages >= MAX_WINDOW_PAGES:
            res.error = f"more than {MAX_WINDOW_PAGES} pages in one day — stopped, will retry"
            log(f"[sync] {st.source}: {res.error}")
            return False


def _read_days(db, s, st, cur: datetime, end: datetime, res: WalkResult, limiter, done,
               budget=None, sleep=time.sleep, log=print) -> bool:
    """Read [cur, end] a day at a time, oldest first, calling done(day_end) after each
    completed day. Days are cur, cur + 1 day, …; every window after the first starts
    SLICE_OVERLAP before its day. True when `end` was reached."""
    edge = cur
    while edge < end:
        if budget is not None and res.requests >= budget:
            res.behind = True
            log(f"[sync] {st.source}: read through {edge:%Y-%m-%d %H:%M} UTC; continuing "
                f"next round")
            return False
        day_end = min(edge + SLICE, end)
        start = edge if edge == cur else edge - SLICE_OVERLAP
        if not walk_window(db, s, st, start, day_end, res, limiter, sleep=sleep, log=log):
            return False
        done(day_end)
        edge = day_end
    return True


def sync_txrec(db, s, st, cfg, now: datetime, res: WalkResult, limiter, budget=None,
               sleep=time.sleep, log=print) -> WalkResult:
    """Read one group from its watermark (minus TXREC_OVERLAP) to now − TXREC_END_LAG."""
    end = now - TXREC_END_LAG
    wm = store.get_watermark(db, st.source)
    start = (wm - TXREC_OVERLAP) if wm else (now - timedelta(days=cfg.initial_sync_days))
    res.completed = _read_days(db, s, st, start, end, res, limiter,
                               lambda day_end: store.set_watermark(db, st.source, day_end),
                               budget=budget, sleep=sleep, log=log)
    return res


def api_state(db, source: str) -> dict:
    """{'mode': 'txrec'|'fallback', 'fails', 'reason', 'since', 'retry_at'} for one group."""
    return store.get_json_meta(db, f"txrec:{source}", {}) or {}


def _save_state(db, source: str, state: dict) -> None:
    store.set_meta(db, f"txrec:{source}", json.dumps(state))


def sync_group(db, s, st, cfg, now: datetime, limiter, budget=None, sleep=time.sleep,
               log=print) -> WalkResult:
    """One group: the transaction-records API, or the economy API while it is unusable.

    FALLBACK_AFTER consecutive unusable answers (403/404/400, or a shape this code does not
    understand) switch the group to the economy API; while there, the new API is tried again
    every TXREC_RETRY_S and the group returns to it on the first good answer. Throttling and
    network errors are unknown answers: they neither count towards the switch nor reset it."""
    state = api_state(db, st.source)
    ts = now.timestamp()
    on_fallback = state.get("mode") == "fallback"
    if not on_fallback or ts >= state.get("retry_at", 0):
        res = WalkResult(st.source, api="txrec")
        try:
            sync_txrec(db, s, st, cfg, now, res, limiter, budget=budget, sleep=sleep, log=log)
        except (roblox.AuthError, roblox.ReadOnlyViolation):
            raise
        except Unusable as e:
            reason = str(e)[:120]
            fails = FALLBACK_AFTER if on_fallback else int(state.get("fails", 0)) + 1
            if fails < FALLBACK_AFTER:
                _save_state(db, st.source, {"mode": "txrec", "fails": fails, "reason": reason})
                res.error = (f"sales API answered {reason} (attempt {fails} of {FALLBACK_AFTER} "
                             f"before switching to the older transactions API)")
                log(f"[sync] {st.source}: {res.error}")
                return res
            state = {"mode": "fallback", "fails": fails, "reason": reason,
                     "since": state.get("since") if on_fallback else int(ts),
                     "retry_at": int(ts + TXREC_RETRY_S)}
            _save_state(db, st.source, state)
            log(f"[sync] {st.source}: sales API answered {reason} — using the older "
                f"transactions API, retrying the new one every {TXREC_RETRY_S // 3600} h")
        except Exception as e:          # noqa: BLE001 — unknown answer: keep data, retry
            if not on_fallback:
                res.error = roblox.safe_error(e)
                log(f"[sync] {st.source}: {res.error}")
                return res
            state = dict(state, retry_at=int(ts + TXREC_RETRY_S))
            _save_state(db, st.source, state)
        else:
            if on_fallback or state.get("fails"):
                if on_fallback:
                    log(f"[sync] {st.source}: the sales API answers again — switched back")
                _save_state(db, st.source, {"mode": "txrec", "fails": 0})
            return res
    res = WalkResult(st.source, api="economy")
    res.fallback = state.get("reason") or "unavailable"
    return _sync_economy(db, s, st, roblox.GROUP_TX_URL.format(id=st.group_id), cfg, now, res,
                         sleep, log)


def sync_stores(db, s, cfg, user_id: int | None, now: datetime | None = None,
                sleep=time.sleep, log=print, limiter=None, budget=None) -> list[WalkResult]:
    """Read every configured store once. Watermarks advance per store, only on completion.
    `budget` caps transaction-records requests per store (the background poller sets it)."""
    limiter = limiter or LIMITER
    results = []
    for i, st in enumerate(cfg.stores):
        if i:
            sleep(STORE_SPACING)
        walk_now = now or datetime.now(timezone.utc)
        if st.group_id is not None:
            res = sync_group(db, s, st, cfg, walk_now, limiter, budget=budget, sleep=sleep,
                             log=log)
        else:
            res = WalkResult(st.source)
            try:
                url = tx_url(st, user_id)
            except ValueError as e:
                res.error = str(e)
            else:
                _sync_economy(db, s, st, url, cfg, walk_now, res, sleep, log)
        results.append(res)
    if any(r.loc_changed for r in results):
        store.bump_epoch(db)            # earlier rows gained a location: dashboards reload
    return results


# ── backfill (owner-started, never automatic) ─────────────────────────────────

def backfill(db, s, cfg, sources=None, since: datetime | None = None,
             until: datetime | None = None, restart: bool = False, now: datetime | None = None,
             limiter=None, sleep=time.sleep, log=print) -> list[tuple[WalkResult, dict]]:
    """Read older sales of each group from the transaction-records API, a day at a time,
    oldest first. Progress is saved after every day (meta 'backfill:<source>'), so a run that
    stops — an error, Ctrl+C — resumes where it stopped. Sales already stored from the
    economy API or a CSV import are matched rather than duplicated, and gain the API's
    location and hold status. It never touches the sync watermarks."""
    limiter = limiter or LIMITER
    now = now or datetime.now(timezone.utc)
    targets = [st for st in cfg.groups if sources is None or st.source in sources]
    out = []
    for st in targets:
        key = f"backfill:{st.source}"
        state = None if restart else store.get_json_meta(db, key)
        if not state:
            state = {"since": (since or BACKFILL_SINCE).isoformat(),
                     "until": (until or now - TXREC_END_LAG).isoformat(), "through": None}
            store.set_meta(db, key, json.dumps(state))
        start = datetime.fromisoformat(state["since"])
        stop = datetime.fromisoformat(state["until"])
        through = datetime.fromisoformat(state["through"]) if state.get("through") else None
        res = WalkResult(st.source, api="txrec")

        def done(day_end, state=state, key=key, st=st, res=res):
            state["through"] = day_end.isoformat()
            store.set_meta(db, key, json.dumps(state))
            log(f"[backfill] {st.source}: through {day_end:%Y-%m-%d %H:%M} UTC "
                f"({res.new:,} new so far)")

        if through is not None and through >= stop:
            res.completed = True
        else:
            try:
                res.completed = _read_days(
                    db, s, st, (through - SLICE_OVERLAP) if through else start, stop, res,
                    limiter, done, sleep=sleep, log=log)
            except (roblox.AuthError, roblox.ReadOnlyViolation):
                raise
            except Unusable as e:
                res.error = f"the sales API cannot serve this group ({e})"
            except Exception as e:      # noqa: BLE001 — progress so far is kept
                res.error = roblox.safe_error(e)
        out.append((res, state))
    if any(r.loc_changed for r, _ in out):
        store.bump_epoch(db)
    return out


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
