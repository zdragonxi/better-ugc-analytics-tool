"""Sales sync over the economy API — the account's own store, and the fallback for groups
whose transaction-records API is unusable: watermarks, truncation, store isolation, back-off,
dedupe. (The transaction-records path is tested in test_txrec.py.)"""
import json
import time
from datetime import timedelta, timezone, datetime

from helpers import (FAKE_COOKIE, GID_A, GID_B, TmpDirCase, days_ago, make_cfg, tx)
from fakes import FakeRoblox, pages

from roblox_sales import roblox, store, sync

NOSLEEP = lambda s: None  # noqa: E731
UID = 4242
PA = f"/v2/groups/{GID_A}/transactions"
PB = f"/v2/groups/{GID_B}/transactions"
PU = f"/v2/users/{UID}/transactions"


def history(n, start_days_ago=1.0, step_minutes=30, base=0, **kw):
    """n synthetic sales, newest first, one every step_minutes going back in time."""
    now = datetime.now(timezone.utc) - timedelta(days=start_days_ago)
    return [tx(base + i, now - timedelta(minutes=step_minutes * i), **kw) for i in range(n)]


class SyncTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp)
        self.s = roblox.make_session(FAKE_COOKIE)
        # both groups on the economy fallback, not due for a retry of the new API
        for gid in (GID_A, GID_B):
            store.set_meta(self.db, f"txrec:group:{gid}", json.dumps(
                {"mode": "fallback", "fails": 3, "reason": "HTTP 404",
                 "retry_at": int(time.time()) + 86400}))

    def run_sync(self, fake):
        with fake:
            return {r.source: r for r in sync.sync_stores(self.db, self.s, self.cfg, UID,
                                                          sleep=NOSLEEP, log=lambda *_: None)}

    def count(self, source=None):
        if source:
            return self.db.execute("SELECT COUNT(*) FROM sales WHERE source=?", (source,)).fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

    def test_first_sync_walks_initial_window_and_sets_watermarks(self):
        fake = FakeRoblox()
        fake.route(PA, pages(history(250, step_minutes=60)))     # ~10 days, 3 pages
        fake.route(PB, pages([]))
        fake.route(PU, pages([]))
        res = self.run_sync(fake)
        self.assertEqual(self.count(f"group:{GID_A}"), 250)
        self.assertTrue(all(r.completed and not r.error for r in res.values()))
        for st in self.cfg.stores:
            self.assertIsNotNone(store.get_watermark(self.db, st.source))

    def test_watermark_is_walk_start_minus_overlap(self):
        fake = FakeRoblox()
        for p in (PA, PB, PU):
            fake.route(p, pages(history(3)))
        before = datetime.now(timezone.utc)
        self.run_sync(fake)
        wm = store.get_watermark(self.db, f"group:{GID_A}")
        self.assertLessEqual(wm, before - sync.OVERLAP + timedelta(seconds=5))
        self.assertGreaterEqual(wm, before - sync.OVERLAP - timedelta(seconds=5))

    def test_truncated_walk_saves_rows_but_never_advances_watermark(self):
        fake = FakeRoblox()
        # 500 sales over ~10 days, but Roblox refuses any cursor past 2 pages
        fake.route(PA, pages(history(500, step_minutes=30), cap_pages=2))
        fake.route(PB, pages(history(5, base=9000)))
        fake.route(PU, pages([]))
        res = self.run_sync(fake)
        self.assertTrue(res[f"group:{GID_A}"].truncated)
        self.assertEqual(self.count(f"group:{GID_A}"), 200)          # what was read is kept
        self.assertIsNone(store.get_watermark(self.db, f"group:{GID_A}"))
        # the healthy store still advanced
        self.assertIsNotNone(store.get_watermark(self.db, f"group:{GID_B}"))

    def test_one_failing_store_does_not_stop_the_others(self):
        fake = FakeRoblox()
        fake.json(PA, {"errors": []}, status=403)
        fake.route(PB, pages(history(7, base=100)))
        fake.route(PU, pages(history(2, base=200)))
        res = self.run_sync(fake)
        self.assertIsNotNone(res[f"group:{GID_A}"].error)
        self.assertIsNone(store.get_watermark(self.db, f"group:{GID_A}"))
        self.assertEqual(self.count(f"group:{GID_B}"), 7)
        self.assertEqual(self.count("user"), 2)
        self.assertIsNotNone(store.get_watermark(self.db, f"group:{GID_B}"))

    def test_interrupted_catch_up_is_rewalked_not_stranded(self):
        """A failure on page 3 leaves the watermark alone, so the next round walks the whole
        gap again instead of stopping at the already-saved newest pages."""
        txs = history(400, step_minutes=30)
        state = {"fail": True}
        good = pages(txs)

        def flaky(params):
            if state["fail"] and params.get("cursor") == "200":
                return 503, {}
            return good(params)
        fake = FakeRoblox()
        fake.route(PA, flaky)
        fake.route(PB, pages([]))
        fake.route(PU, pages([]))
        res = self.run_sync(fake)
        self.assertIsNotNone(res[f"group:{GID_A}"].error)
        self.assertEqual(self.count(f"group:{GID_A}"), 200)
        state["fail"] = False
        res = self.run_sync(fake)
        self.assertTrue(res[f"group:{GID_A}"].completed)
        self.assertEqual(self.count(f"group:{GID_A}"), 400)

    def test_second_sync_stops_at_watermark(self):
        fake = FakeRoblox()
        fake.route(PA, pages(history(300, start_days_ago=0.01, step_minutes=2)))
        fake.route(PB, pages([]))
        fake.route(PU, pages([]))
        self.run_sync(fake)
        fake.calls.clear()
        self.run_sync(fake)
        a_calls = [c for c in fake.calls if PA in c[1]]
        self.assertEqual(len(a_calls), 1)        # page 1 already reaches the watermark

    def test_empty_page_is_not_trusted_once_rows_exist(self):
        fake = FakeRoblox()
        for p in (PA, PB, PU):
            fake.route(p, pages(history(3)))
        self.run_sync(fake)
        wm1 = store.get_watermark(self.db, f"group:{GID_A}")
        fake.route(PA, pages([]))                # Roblox suddenly says "no sales at all"
        res = self.run_sync(fake)
        self.assertIsNotNone(res[f"group:{GID_A}"].error)
        self.assertEqual(store.get_watermark(self.db, f"group:{GID_A}"), wm1)
        self.assertEqual(self.count(f"group:{GID_A}"), 3)

    def test_429_backs_off_then_succeeds(self):
        waits = []
        seq = iter([(429, {}), (429, {}), (200, {"data": history(2), "nextPageCursor": None})])
        fake = FakeRoblox()
        fake.route(PA, lambda p: next(seq))
        with fake:
            res = sync.walk_store(self.db, self.s, "https://economy.roblox.com" + PA,
                                  f"group:{GID_A}", days_ago(30), sleep=waits.append,
                                  log=lambda *_: None)
        self.assertTrue(res.completed)
        self.assertEqual(waits[:2], [30, 60])

    def test_persistent_429_is_unknown_not_empty(self):
        fake = FakeRoblox()
        fake.json(PA, {}, status=429)
        with fake, self.assertRaises(roblox.RobloxUnavailable):
            sync.walk_store(self.db, self.s, "https://economy.roblox.com" + PA, f"group:{GID_A}",
                            days_ago(30), sleep=NOSLEEP, log=lambda *_: None)

    def test_401_raises_auth_error_for_the_whole_round(self):
        fake = FakeRoblox()
        fake.json(PA, {}, status=401)
        with self.assertRaises(roblox.AuthError):
            self.run_sync(fake)

    def test_api_upsert_is_idempotent_and_keys_on_idhash(self):
        t = tx(1, days_ago(1))
        self.assertTrue(store.upsert_api_tx(self.db, t, "group:1"))
        self.assertFalse(store.upsert_api_tx(self.db, t, "group:1"))
        t2 = dict(t, isPending=False)
        store.upsert_api_tx(self.db, t2, "group:1")
        self.assertEqual(self.count(), 1)

    def test_rentals_are_separate_sales(self):
        base = days_ago(2)
        store.upsert_api_tx(self.db, tx(1, base, robux=3, buyer=5, asset=9), "group:1")
        store.upsert_api_tx(self.db, tx(2, base + timedelta(seconds=20), robux=30, buyer=5, asset=9),
                            "group:1")
        self.assertEqual(self.count(), 2)
