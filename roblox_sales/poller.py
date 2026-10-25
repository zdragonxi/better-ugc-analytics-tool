"""The background thread that keeps the database current (sales, financials, balance).

Failure policy: any failure keeps the last-known data on screen and is reported in the
dashboard's status line. Nothing here ever deletes or blanks stored data because a request
failed — an unreadable answer is unknown, not zero.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone

from . import catalog, roblox, store, sync

TICK = 15               # seconds between scheduler checks
AUTH_RETRY = 900        # after a 401, wait this long before trying the cookie again
FORCED_SYNC_FLOOR = 30  # a dashboard "refresh now" never syncs more often than this
CATALOG_FLOOR = 60      # an on-demand catalog refresh never runs more often than this
BURST_GAP = 30          # seconds kept between a sales sync and a catalog walk, either way


def status_message(results) -> str | None:
    """The dashboard's status line after a sync round, or None when all is well."""
    msgs = []
    errs = [f"{r.source}: {r.error}" for r in results if r.error]
    cut = [r.source for r in results if r.truncated]
    if errs:
        msgs.append("; ".join(errs))
    elif cut:
        msgs.append("history gap: Roblox stopped paginating for " + ", ".join(cut)
                    + " — import that month's CSV export")
    fb = [r for r in results if r.fallback]
    if fb:
        msgs.append(", ".join(f"{r.source} ({r.fallback})" for r in fb)
                    + ": sales API unavailable — using the older transactions API (no "
                      "in-game/website split); retrying every "
                    + f"{sync.TXREC_RETRY_S // 3600} h")
    return "; ".join(msgs)[:300] if msgs else None


class Poller:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.state = {"polling": False, "last_sync": None, "sync_error": None,
                      "stores": {}, "signed_in": False}
        self._wake = threading.Event()
        self._force = False
        self._force_catalog = False
        self._stop = threading.Event()
        self._thread = None

    # called by the web server when a dashboard opens
    def refresh_now(self):
        self._force = True
        self._wake.set()

    def refresh_catalog_now(self):
        self._force_catalog = True
        self._wake.set()

    def start(self):
        if not self.cfg.cookie():
            self.state["sync_error"] = ("no cookie configured — showing stored data only "
                                        "(see README: Configure)")
            return
        if not self.cfg.stores:
            self.state["sync_error"] = "no stores configured — add a [group:<id>] section"
            return
        self.state["polling"] = True
        self._thread = threading.Thread(target=self._supervise, name="poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _supervise(self):
        while not self._stop.is_set():
            try:
                self._run()
            except Exception:           # noqa: BLE001 — never let the thread die silently
                traceback.print_exc()
                self.state["sync_error"] = "internal error in the poller — restarting in 60 s"
            if self._stop.wait(60):
                return

    def _run(self):
        cfg = self.cfg
        db = store.connect(cfg.db_path)
        s = roblox.make_session(cfg.cookie(), cfg.proxy)
        user_id = None
        last = {"sync": 0.0, "fin": 0.0, "bal": 0.0, "cat": 0.0, "burst_end": 0.0}
        while not self._stop.is_set():
            try:
                if user_id is None:
                    user_id = roblox.authenticated_user(s)["id"]
                    self.state["signed_in"] = True
                    self.log("[poller] cookie accepted; syncing")
                now = time.time()
                if now - last["fin"] >= cfg.financials_interval and cfg.groups:
                    sync.refresh_financials(db, s, cfg)
                    last["fin"] = time.time()
                if now - last["bal"] >= cfg.balance_interval:
                    bal = sync.fetch_balance(s)
                    if bal is not None:
                        store.set_balance(db, bal)      # a failed read keeps the last value
                    last["bal"] = time.time()
                forced, self._force = self._force, False
                # the sales walk and the catalog walk never run back to back: they share
                # one per-account budget, and bursts are what earn long 429 penalties
                since_burst = time.time() - last["burst_end"]
                if since_burst >= BURST_GAP and (
                        now - last["sync"] >= cfg.sync_interval or
                        (forced and now - last["sync"] >= FORCED_SYNC_FLOOR)):
                    results = sync.sync_stores(db, s, cfg, user_id, log=self.log,
                                               budget=sync.POLL_BUDGET)
                    last["sync"] = last["burst_end"] = time.time()
                    self.state["stores"] = {r.source: r.as_dict() for r in results}
                    self.state["last_sync"] = datetime.now(timezone.utc).isoformat()
                    self.state["sync_error"] = status_message(results)
                elapsed_cat = time.time() - last["cat"]
                if cfg.groups and time.time() - last["burst_end"] >= BURST_GAP and (
                        elapsed_cat >= cfg.catalog_interval or
                        (self._force_catalog and elapsed_cat >= CATALOG_FLOOR)):
                    self._force_catalog = False
                    snap = catalog.refresh_all(db, s, cfg)
                    last["cat"] = last["burst_end"] = time.time()
                    bad = [k for k, v in snap.items() if v.get("error")]
                    if bad:
                        self.log(f"[catalog] incomplete for {', '.join(bad)} — kept the last "
                                 f"complete count")
            except roblox.AuthError as e:
                self.state.update(signed_in=False, sync_error=str(e))
                user_id = None
                self.log(f"[poller] {e}; retrying in {AUTH_RETRY // 60} min")
                if self._stop.wait(AUTH_RETRY):
                    break
                continue
            except roblox.ReadOnlyViolation:
                raise
            except Exception as e:      # noqa: BLE001 — transport/proxy/5xx: keep last data
                self.state["sync_error"] = f"Roblox unreachable ({type(e).__name__}) — " \
                                           f"showing stored data, retrying"
                self.log(f"[poller] {roblox.safe_error(e)}")
            self._wake.wait(TICK)
            self._wake.clear()
        db.close()
