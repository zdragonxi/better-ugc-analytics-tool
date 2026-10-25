"""The transaction-records API path for groups: the shared rate limiter, row mapping, the
time-window sync and its watermark, the no-duplicate cutover from economy / CSV rows, the
fallback to the economy API, and the resumable backfill. Responses are faked at the HTTP
adapter layer in the recorded shapes; everything is synthetic."""
import json
import os
from datetime import datetime, timedelta, timezone

from helpers import (FAKE_COOKIE, GID_A, GID_B, ROOT, FakeClock, TmpDirCase, make_cfg, tx,
                     txrow, write_csv)
from fakes import FakeRoblox, pages, txrec_pages, txrec_path

from roblox_sales import poller, roblox, store, sync, web

NOSLEEP = lambda s: None  # noqa: E731
UID = 4242
SA, SB = f"group:{GID_A}", f"group:{GID_B}"
NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def ms_param(url, key):
    from urllib.parse import parse_qs, urlsplit
    return int(parse_qs(urlsplit(url).query)[key][0])


def as_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ── the shared limiter ──────────────────────────────────────────────────────────

class LimiterTests(TmpDirCase):
    def test_own_cap_is_per_minute(self):
        c = FakeClock()
        lim = roblox.RateLimiter(per_minute=40, clock=c.now, sleep=c.sleep)
        for _ in range(40):
            lim.acquire()
        self.assertEqual(c.slept, [])
        lim.acquire()                                   # the 41st waits for the window
        self.assertEqual(c.slept, [60.0])

    def test_remaining_at_reserve_waits_for_reset(self):
        c = FakeClock()
        lim = roblox.RateLimiter(per_minute=40, reserve=2, clock=c.now, sleep=c.sleep)
        lim.observe(200, {"x-ratelimit-remaining": "3", "x-ratelimit-reset": "40"})
        lim.acquire()
        self.assertEqual(c.slept, [])                   # above the reserve: no wait
        lim.observe(200, {"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "17"})
        lim.acquire()
        self.assertEqual(c.slept, [17.5])

    def test_429_retry_after_pauses_every_caller(self):
        c = FakeClock()
        lim = roblox.RateLimiter(clock=c.now, sleep=c.sleep)
        lim.observe(429, {"retry-after": "5", "x-ratelimit-reset": "5"})
        lim.acquire()
        self.assertEqual(c.slept, [5.5])

    def test_header_parsing_and_cap(self):
        self.assertEqual(roblox._header_num("50, 50;w=60"), 50)
        self.assertIsNone(roblox._header_num(None))
        c = FakeClock()
        lim = roblox.RateLimiter(clock=c.now, sleep=c.sleep)
        lim.observe(429, {"retry-after": "86400"})      # absurd: capped
        lim.acquire()
        self.assertEqual(c.slept, [roblox.RateLimiter.MAX_PAUSE + 0.5])

    def test_get_json_obeys_retry_after_instead_of_fixed_backoff(self):
        c = FakeClock()
        lim = roblox.RateLimiter(clock=c.now, sleep=c.sleep)
        seq = iter([(429, {}, {"retry-after": "5"}),
                    (200, {"ok": 1}, {"x-ratelimit-remaining": "48", "x-ratelimit-reset": "50"})])
        fake = FakeRoblox()
        fake.route("/x", lambda p: next(seq))
        fixed = []
        with fake:
            st, j = roblox.get_json(roblox.make_session(FAKE_COOKIE), "https://apis.roblox.com/x",
                                    limiter=lim, sleep=fixed.append)
        self.assertEqual((st, j), (200, {"ok": 1}))
        self.assertEqual(fixed, [])                     # no 30 s economy back-off
        self.assertEqual(c.slept, [5.5])


# ── mapping ─────────────────────────────────────────────────────────────────────

class MappingTests(TmpDirCase):
    def test_location_hold_amount_price(self):
        t = NOW - timedelta(hours=1)
        web_ = store.parse_txrec(txrow(t, robux=21, price=70, loc=1, hold="Active"))
        game = store.parse_txrec(txrow(t, loc=2, hold="Released"))
        odd = store.parse_txrec(txrow(t, loc=7, hold="Mystery"))
        self.assertEqual((web_["location"], web_["pending"], web_["robux"], web_["price"]),
                         ("Website", 1, 21, 70))
        self.assertEqual((game["location"], game["pending"]), ("Game", 0))
        self.assertEqual((odd["location"], odd["pending"]), (None, None))
        self.assertEqual(web_["buyer_id"], 70001)
        self.assertEqual(web_["created"], store.canon(t))

    def test_unreadable_rows_are_schema_errors(self):
        t = NOW
        for bad in (dict(txrow(t), createdTime=None), dict(txrow(t), amount=None),
                    dict(txrow(t), amount="lots"), "not a row"):
            with self.assertRaises(store.SchemaError):
                store.parse_txrec(bad)

    def test_asset_falls_back_to_product_target(self):
        row = txrow(NOW)
        row["details"] = dict(row["details"], assetId=None, productTargetId=777)
        self.assertEqual(store.parse_txrec(row)["asset_id"], 777)

    def test_synthesized_id_is_stable_and_amount_sensitive(self):
        a = store.parse_txrec(txrow(NOW, robux=21))
        self.assertEqual(store.txrec_id(SA, a), store.txrec_id(SA, dict(a)))
        b = store.parse_txrec(txrow(NOW, robux=2))     # a rental payment, same second
        self.assertNotEqual(store.txrec_id(SA, a), store.txrec_id(SA, b))
        later = store.parse_txrec(dict(txrow(NOW), updatedTime="2026-12-01T00:00:00.000Z",
                                       holdStatus="Active"))
        self.assertEqual(store.txrec_id(SA, a), store.txrec_id(SA, later))  # hold flips: same sale


# ── shared fixture for the sync tests ─────────────────────────────────────────

class TxrecCase(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp, user=False)
        self.s = roblox.make_session(FAKE_COOKIE)
        self.clock = FakeClock()
        self.lim = roblox.RateLimiter(per_minute=40, clock=self.clock.now, sleep=self.clock.sleep)
        self.fake = FakeRoblox()
        self.fake.route(txrec_path(GID_B), txrec_pages([]))

    def run_sync(self, now=NOW, budget=None):
        with self.fake:
            return {r.source: r for r in sync.sync_stores(
                self.db, self.s, self.cfg, UID, now=now, sleep=NOSLEEP, log=lambda *_: None,
                limiter=self.lim, budget=budget)}

    def count(self, source=SA):
        return self.db.execute("SELECT COUNT(*) FROM sales WHERE source=?", (source,)).fetchone()[0]

    def calls(self, gid=GID_A):
        return [u for _, u, _ in self.fake.calls if txrec_path(gid) in u]


class WindowSyncTests(TxrecCase):
    def test_first_sync_reads_initial_days_and_sets_watermark(self):
        self.cfg.initial_sync_days = 3
        rows = [txrow(NOW - timedelta(minutes=20 * i + 2), buyer=1000 + i,
                      loc=1 if i % 4 == 0 else 2, hold="Active" if i < 30 else "Released")
                for i in range(200)]                    # ~2.8 days
        self.fake.route(txrec_path(GID_A), txrec_pages(rows))
        res = self.run_sync()
        r = res[SA]
        self.assertTrue(r.completed and not r.error, r.as_dict())
        self.assertEqual(r.api, "txrec")
        self.assertEqual(self.count(), 200)
        self.assertEqual(store.get_watermark(self.db, SA), NOW - sync.TXREC_END_LAG)
        # one window per day, oldest first, each covering at most a day
        calls = self.calls()
        self.assertEqual(len(calls), 3)                 # the 3rd day ends 60 s before now
        starts = [ms_param(u, "startTimeMillis") for u in calls]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(as_dt(starts[0]), NOW - timedelta(days=3))
        for u in calls:
            self.assertIn("ledgerReason=SaleOfGood", u)
            self.assertIn("limit=100", u)
            self.assertLessEqual(ms_param(u, "endTimeMillis") - ms_param(u, "startTimeMillis"),
                                 86_401_000)            # a day + the 1 s overlap
        hdrs = [h for _, u, h in self.fake.calls if txrec_path(GID_A) in u][0]
        self.assertEqual(hdrs.get("Origin"), "https://create.roblox.com")
        # location and hold are stored
        locs = dict(self.db.execute("SELECT location, COUNT(*) FROM sales GROUP BY location"))
        self.assertEqual(locs, {"Website": 50, "Game": 150})
        self.assertEqual(self.db.execute("SELECT SUM(is_pending) FROM sales").fetchone()[0], 30)
        self.assertEqual(self.lim.waited, 0)            # 5 requests: well inside the budget

    def test_cursor_walk_until_hasMore_false(self):
        self.cfg.initial_sync_days = 1
        rows = [txrow(NOW - timedelta(minutes=5 + i), buyer=i) for i in range(250)]
        self.fake.route(txrec_path(GID_A), txrec_pages(rows))
        res = self.run_sync()
        self.assertEqual(self.count(), 250)
        self.assertEqual(res[SA].pages, 3)
        self.assertEqual([("cursor=" in u) for u in self.calls()], [False, True, True])

    def test_second_sync_starts_at_watermark_minus_overlap(self):
        self.cfg.initial_sync_days = 1
        rows = [txrow(NOW - timedelta(minutes=3 + i), buyer=i) for i in range(30)]
        self.fake.route(txrec_path(GID_A), txrec_pages(rows))
        self.run_sync()
        wm = store.get_watermark(self.db, SA)
        self.fake.calls.clear()
        later = NOW + timedelta(minutes=3)
        rows.append(txrow(later - timedelta(minutes=1, seconds=30), buyer=999))
        self.run_sync(now=later)
        (u,) = self.calls()
        self.assertEqual(as_dt(ms_param(u, "startTimeMillis")), wm - sync.TXREC_OVERLAP)
        self.assertEqual(as_dt(ms_param(u, "endTimeMillis")), later - sync.TXREC_END_LAG)
        self.assertEqual(self.count(), 31)              # the overlap re-read added nothing twice

    def test_failed_day_keeps_finished_days_and_is_read_again(self):
        self.cfg.initial_sync_days = 3
        rows = [txrow(NOW - timedelta(hours=i, minutes=7), buyer=i) for i in range(70)]
        good = txrec_pages(rows)
        state = {"fail": True}
        day2 = NOW - timedelta(days=2)

        def flaky(params):
            if state["fail"] and as_dt(int(params["startTimeMillis"])) >= day2 - timedelta(seconds=2) \
                    and as_dt(int(params["startTimeMillis"])) < day2 + timedelta(hours=1):
                return 503, {}
            return good(params)
        self.fake.route(txrec_path(GID_A), flaky)
        res = self.run_sync()
        self.assertIsNotNone(res[SA].error)
        self.assertFalse(res[SA].completed)
        self.assertEqual(store.get_watermark(self.db, SA), day2)     # day 1 kept
        first = self.count()
        self.assertGreater(first, 0)
        state["fail"] = False
        res = self.run_sync()
        self.assertTrue(res[SA].completed, res[SA].as_dict())
        self.assertEqual(self.count(), 70)
        self.assertEqual(store.get_watermark(self.db, SA), NOW - sync.TXREC_END_LAG)

    def test_empty_window_is_not_trusted_where_sales_are_stored(self):
        self.cfg.initial_sync_days = 1
        rows = [txrow(NOW - timedelta(minutes=2), buyer=1), txrow(NOW - timedelta(hours=3), buyer=2)]
        self.fake.route(txrec_path(GID_A), txrec_pages(rows))
        self.run_sync()
        wm = store.get_watermark(self.db, SA)
        self.fake.route(txrec_path(GID_A), txrec_pages([]))     # Roblox suddenly says "none"
        res = self.run_sync(now=NOW + timedelta(minutes=3))
        self.assertIn("not trusted", res[SA].error)
        self.assertEqual(store.get_watermark(self.db, SA), wm)
        self.assertEqual(self.count(), 2)

    def test_empty_window_is_believed_for_a_quiet_store(self):
        self.fake.route(txrec_path(GID_A), txrec_pages([]))
        res = self.run_sync()
        self.assertTrue(res[SA].completed and not res[SA].error)
        self.assertEqual(store.get_watermark(self.db, SA), NOW - sync.TXREC_END_LAG)

    def test_empty_page_that_says_more_follow_is_not_trusted(self):
        self.fake.route(txrec_path(GID_A), lambda p: (200, {"transactions": [], "nextCursor": "x",
                                                            "previousCursor": None, "hasMore": True}))
        res = self.run_sync()
        self.assertIsNotNone(res[SA].error)
        self.assertIsNone(store.get_watermark(self.db, SA))

    def test_round_budget_stops_between_days_without_error(self):
        self.cfg.initial_sync_days = 5
        rows = [txrow(NOW - timedelta(hours=3 * i + 1), buyer=i) for i in range(39)]
        self.fake.route(txrec_path(GID_A), txrec_pages(rows))
        res = self.run_sync(budget=2)
        r = res[SA]
        self.assertTrue(r.behind and not r.error and not r.completed, r.as_dict())
        self.assertEqual(store.get_watermark(self.db, SA), NOW - timedelta(days=3))
        self.assertEqual(poller.status_message(res.values()), None)   # catching up is not an error
        res = self.run_sync(budget=10)
        self.assertTrue(res[SA].completed)
        self.assertEqual(self.count(), 39)

    def test_429_inside_a_window_waits_retry_after_and_completes(self):
        self.cfg.initial_sync_days = 1
        rows = [txrow(NOW - timedelta(minutes=9 + i), buyer=i) for i in range(150)]
        good = txrec_pages(rows)
        seen = {"n": 0}

        def throttled(params):
            seen["n"] += 1
            if seen["n"] == 2:
                return 429, {"errors": []}, {"retry-after": "7"}
            return good(params)
        self.fake.route(txrec_path(GID_A), throttled)
        res = self.run_sync()
        self.assertTrue(res[SA].completed)
        self.assertEqual(self.count(), 150)
        self.assertEqual(self.clock.slept, [7.5])

    def test_location_reaches_the_dashboard_rows(self):
        self.cfg.initial_sync_days = 1
        self.fake.route(txrec_path(GID_A), txrec_pages([txrow(NOW - timedelta(hours=1), loc=1),
                                                        txrow(NOW - timedelta(hours=2), loc=2, buyer=5)]))
        self.run_sync()
        data = web.read_data(self.db, self.cfg, {})
        self.assertEqual(sorted(r[5] for r in data["rows"]), ["Game", "Website"])


# ── cutover: no duplicates against economy-API and CSV rows ─────────────────────

class CutoverTests(TxrecCase):
    def setUp(self):
        super().setUp()
        self.cfg.initial_sync_days = 1

    def sync_rows(self, rows, now=NOW):
        self.fake.route(txrec_path(GID_A), txrec_pages(rows))
        return self.run_sync(now=now)

    def test_no_duplicates_against_economy_and_csv_rows(self):
        base = NOW - timedelta(hours=5)
        # three sales already stored by the economy API, one by a CSV import
        for i in range(3):
            store.upsert_api_tx(self.db, tx(i, base + timedelta(minutes=i), robux=21, asset=500,
                                            buyer=100 + i), SA)
        csv_t = base + timedelta(minutes=30)
        self.db.execute("INSERT INTO sales (id, source, created, asset_id, buyer_id, robux, "
                        "is_pending, location, origin) VALUES "
                        "('csv-1', ?, ?, 600, 200, 9, 0, 'Website', 'csv')", (SA, store.canon(csv_t)))
        self.db.commit()
        epoch = store.rows_epoch(self.db)
        # the same four sales as the new API reports them (its clock 0.5-9 s after the
        # economy API's, ~0.1 s before the CSV's), plus one sale nobody stored yet
        rows = [txrow(base + timedelta(minutes=i, seconds=d), robux=21, asset=500, buyer=100 + i,
                      loc=2) for i, d in ((0, 0.5), (1, 1.5), (2, 9))]
        rows.append(txrow(csv_t - timedelta(milliseconds=100), robux=9, asset=600, buyer=200, loc=1))
        rows.append(txrow(base + timedelta(hours=1), robux=21, asset=500, buyer=300))
        self.sync_rows(rows)
        self.assertEqual(self.count(), 5)               # 4 matched + 1 new
        origins = dict(self.db.execute("SELECT origin, COUNT(*) FROM sales GROUP BY origin"))
        self.assertEqual(origins, {"api": 3, "csv": 1, "txrec": 1})
        # the economy rows gained the location they never had, and dashboards reload
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales WHERE origin='api' "
                                         "AND location='Game'").fetchone()[0], 3)
        self.assertNotEqual(store.rows_epoch(self.db), epoch)
        # reading the same window again (the overlap) changes nothing
        epoch2 = store.rows_epoch(self.db)
        self.sync_rows(rows, now=NOW + timedelta(minutes=3))
        self.assertEqual(self.count(), 5)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sale_alias").fetchone()[0], 4)
        self.assertEqual(store.rows_epoch(self.db), epoch2)

    def test_two_real_sales_seconds_apart_pair_one_to_one(self):
        t = NOW - timedelta(hours=2)
        store.upsert_api_tx(self.db, tx(1, t, robux=21, buyer=7), SA)
        store.upsert_api_tx(self.db, tx(2, t + timedelta(seconds=5), robux=21, buyer=7), SA)
        self.db.commit()
        self.sync_rows([txrow(t + timedelta(seconds=1), buyer=7),
                        txrow(t + timedelta(seconds=6), buyer=7)])
        self.assertEqual(self.count(), 2)

    def test_one_stored_sale_absorbs_only_one_new_row(self):
        t = NOW - timedelta(hours=2)
        store.upsert_api_tx(self.db, tx(1, t, robux=21, buyer=7), SA)
        self.db.commit()
        self.sync_rows([txrow(t + timedelta(seconds=1), buyer=7),
                        txrow(t + timedelta(seconds=4), buyer=7)])
        self.assertEqual(self.count(), 2)               # one matched, one genuinely new

    def test_rental_payments_stay_separate(self):
        t = NOW - timedelta(hours=2)
        store.upsert_api_tx(self.db, tx(1, t, robux=30, buyer=7), SA)
        self.db.commit()
        self.sync_rows([txrow(t + timedelta(seconds=1), robux=30, buyer=7),
                        txrow(t + timedelta(seconds=2), robux=3, buyer=7)])
        self.assertEqual(self.count(), 2)

    def test_beyond_the_match_window_is_a_different_sale(self):
        t = NOW - timedelta(hours=2)
        store.upsert_api_tx(self.db, tx(1, t, robux=21, buyer=7), SA)
        self.db.commit()
        self.sync_rows([txrow(t + timedelta(seconds=store.MATCH_WINDOW_S + 5), buyer=7)])
        self.assertEqual(self.count(), 2)

    def test_new_api_rows_never_merge_with_each_other(self):
        t = NOW - timedelta(hours=2)
        self.sync_rows([txrow(t, buyer=7), txrow(t + timedelta(seconds=3), buyer=7)])
        self.assertEqual(self.count(), 2)

    def test_csv_month_reimport_after_a_match_keeps_one_row(self):
        t = datetime(2026, 9, 20, 6, 0, 0, tzinfo=timezone.utc)
        csv = write_csv(os.path.join(self.tmp, f"Sales_Group{GID_A}_2026-09.csv"),
                        [{"id": "C1", "when": t, "asset": 500, "buyer": 7, "robux": 21}])
        store.import_csv(self.db, csv, [GID_A, GID_B])
        rows = [txrow(t - timedelta(milliseconds=100), buyer=7, loc=1)]
        self.sync_rows(rows)
        self.assertEqual(self.count(), 1)
        store.import_csv(self.db, csv, [GID_A, GID_B])       # month replaced, same Ids
        self.sync_rows(rows, now=NOW + timedelta(minutes=3))
        self.assertEqual(self.count(), 1)

    def test_economy_rows_arriving_after_new_api_rows_are_not_added(self):
        t = NOW - timedelta(hours=2)
        self.sync_rows([txrow(t + timedelta(seconds=2), buyer=7)])
        self.assertFalse(store.upsert_api_tx(self.db, tx(1, t, robux=21, buyer=7), SA))
        self.assertEqual(self.count(), 1)


# ── fallback to the economy API ─────────────────────────────────────────────────

class FallbackTests(TxrecCase):
    def setUp(self):
        super().setUp()
        self.cfg.initial_sync_days = 1
        self.econ = [tx(i, NOW - timedelta(hours=i + 1)) for i in range(4)]
        self.fake.route(f"/v2/groups/{GID_A}/transactions", pages(self.econ))
        self.fake.route(f"/v2/groups/{GID_B}/transactions", pages([]))

    def econ_calls(self):
        return [u for _, u, _ in self.fake.calls if u.split("?")[0].endswith(
            f"economy.roblox.com/v2/groups/{GID_A}/transactions")]

    def test_persistent_404_switches_to_the_economy_api_after_three_rounds(self):
        self.fake.json(txrec_path(GID_A), {"errors": [{"message": "NotFound"}]}, status=404)
        for n in (1, 2):
            res = self.run_sync(now=NOW + timedelta(minutes=3 * n))
            self.assertIn(f"attempt {n} of 3", res[SA].error)
            self.assertEqual(self.econ_calls(), [])
        res = self.run_sync(now=NOW + timedelta(minutes=9))
        r = res[SA]
        self.assertEqual((r.api, r.fallback, r.error), ("economy", "HTTP 404", None))
        self.assertEqual(self.count(), 4)                    # the round still synced
        msg = poller.status_message(res.values())
        self.assertIn(SA, msg)
        self.assertIn("older transactions API", msg)
        self.assertEqual(sync.api_state(self.db, SA)["mode"], "fallback")
        self.assertEqual(res[SB].api, "txrec")              # the other group is unaffected
        # until the retry is due, the new API is not asked at all
        self.fake.calls.clear()
        self.run_sync(now=NOW + timedelta(hours=1))
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.econ_calls()), 1)

    def test_a_schema_change_counts_like_a_404(self):
        self.fake.json(txrec_path(GID_A), {"data": [], "nextPageCursor": None})   # old shape
        for n in range(3):
            res = self.run_sync(now=NOW + timedelta(minutes=3 * n))
        self.assertEqual(res[SA].fallback, "unexpected answer shape")

    def test_throttling_and_5xx_never_count_towards_the_switch(self):
        self.fake.json(txrec_path(GID_A), {}, status=503)
        for n in range(4):
            res = self.run_sync(now=NOW + timedelta(minutes=3 * n))
            self.assertEqual(res[SA].api, "txrec")
            self.assertIsNotNone(res[SA].error)
        self.assertEqual(sync.api_state(self.db, SA), {})
        self.assertEqual(self.econ_calls(), [])

    def test_a_good_answer_resets_the_count(self):
        self.fake.json(txrec_path(GID_A), {}, status=403)
        self.run_sync(now=NOW)
        self.run_sync(now=NOW + timedelta(minutes=3))
        self.fake.route(txrec_path(GID_A), txrec_pages([]))
        self.run_sync(now=NOW + timedelta(minutes=6))
        self.assertEqual(sync.api_state(self.db, SA)["fails"], 0)

    def test_due_retry_returns_to_the_new_api(self):
        store.set_meta(self.db, f"txrec:{SA}", json.dumps(
            {"mode": "fallback", "fails": 3, "reason": "HTTP 404",
             "retry_at": int((NOW - timedelta(minutes=1)).timestamp())}))
        self.fake.route(txrec_path(GID_A), txrec_pages([txrow(NOW - timedelta(hours=2))]))
        res = self.run_sync()
        self.assertEqual((res[SA].api, res[SA].fallback), ("txrec", None))
        self.assertEqual(sync.api_state(self.db, SA)["mode"], "txrec")
        self.assertEqual(self.econ_calls(), [])
        self.assertIsNone(poller.status_message(res.values()))

    def test_failed_retry_waits_again_and_keeps_syncing_on_the_economy_api(self):
        store.set_meta(self.db, f"txrec:{SA}", json.dumps(
            {"mode": "fallback", "fails": 3, "reason": "HTTP 404", "since": 1,
             "retry_at": int((NOW - timedelta(minutes=1)).timestamp())}))
        self.fake.json(txrec_path(GID_A), {}, status=404)
        res = self.run_sync()
        self.assertEqual(res[SA].api, "economy")
        self.assertEqual(self.count(), 4)
        st = sync.api_state(self.db, SA)
        self.assertEqual(st["retry_at"], int(NOW.timestamp()) + sync.TXREC_RETRY_S)
        self.assertEqual(st["since"], 1)


# ── backfill ────────────────────────────────────────────────────────────────────

class BackfillTests(TxrecCase):
    def run_backfill(self, **kw):
        kw.setdefault("since", NOW - timedelta(days=5))
        kw.setdefault("until", NOW)
        with self.fake:
            return sync.backfill(self.db, self.s, self.cfg, now=NOW, limiter=self.lim,
                                 sleep=NOSLEEP, log=lambda *_: None, **kw)

    def test_resumes_where_it_stopped(self):
        rows = [txrow(NOW - timedelta(hours=2 * i + 1), buyer=i) for i in range(55)]  # ~4.6 days
        good = txrec_pages(rows)
        state = {"fail": True}
        day3 = NOW - timedelta(days=3)

        def flaky(params):
            start = as_dt(int(params["startTimeMillis"]))
            if state["fail"] and day3 - timedelta(seconds=2) <= start < day3 + timedelta(hours=1):
                return 503, {}
            return good(params)
        self.fake.route(txrec_path(GID_A), flaky)
        out = dict((r.source, (r, st)) for r, st in self.run_backfill(sources=[SA]))
        r, st = out[SA]
        self.assertIsNotNone(r.error)
        self.assertEqual(st["through"], day3.isoformat())
        state["fail"] = False
        self.fake.calls.clear()
        r, st = self.run_backfill(sources=[SA])[0]
        self.assertTrue(r.completed and not r.error)
        first = ms_param(self.calls()[0], "startTimeMillis")
        self.assertEqual(as_dt(first), day3 - sync.SLICE_OVERLAP)    # resumed, not restarted
        self.assertEqual(self.count(), 55)

    def test_complete_backfill_is_a_noop_and_restart_reads_again(self):
        self.fake.route(txrec_path(GID_A), txrec_pages([txrow(NOW - timedelta(days=2))]))
        self.run_backfill()
        self.fake.calls.clear()
        out = self.run_backfill()
        self.assertTrue(all(r.completed and r.requests == 0 for r, _ in out))
        self.assertEqual(self.fake.calls, [])
        out = self.run_backfill(restart=True, since=NOW - timedelta(days=1))
        self.assertEqual(len(self.calls()), 1)

    def test_groups_only_paced_by_the_limiter_and_watermarks_untouched(self):
        cfg = make_cfg(self.tmp, user=True)
        self.cfg = cfg
        self.fake.route(txrec_path(GID_A), txrec_pages([txrow(NOW - timedelta(days=1))]))
        sent = []
        orig = self.lim.acquire
        self.lim.acquire = lambda: (sent.append(1), orig())[1]
        out = self.run_backfill()
        self.assertEqual(sorted(r.source for r, _ in out), [SA, SB])     # no 'user'
        self.assertEqual(len(sent), sum(r.requests for r, _ in out))
        self.assertEqual(len(sent), 10)                  # 5 day-windows × 2 groups
        for src in (SA, SB, "user"):
            self.assertIsNone(store.get_watermark(self.db, src))

    def test_fills_location_into_older_rows(self):
        # t is exactly a day boundary: the economy row closes one day-window, its twin from
        # the new API (2 s later) opens the next. The first window is empty but must still
        # be believed — otherwise this backfill could never get past that day.
        t = NOW - timedelta(days=2)
        store.upsert_api_tx(self.db, tx(1, t, robux=21, buyer=7), SA)
        self.db.commit()
        epoch = store.rows_epoch(self.db)
        self.fake.route(txrec_path(GID_A), txrec_pages([txrow(t + timedelta(seconds=2), buyer=7,
                                                              loc=1, hold="Active")]))
        self.run_backfill(sources=[SA])
        self.assertEqual(self.db.execute("SELECT location, is_pending, price FROM sales").fetchall(),
                         [("Website", 1, 70)])
        self.assertNotEqual(store.rows_epoch(self.db), epoch)

    def test_the_poller_never_starts_a_backfill(self):
        with open(os.path.join(ROOT, "roblox_sales", "poller.py"), encoding="utf-8") as f:
            self.assertNotIn("backfill", f.read())


class MigrationTests(TmpDirCase):
    def test_an_older_database_gains_the_new_column(self):
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE sales (id TEXT PRIMARY KEY, source TEXT NOT NULL, created TEXT "
                    "NOT NULL, asset_id INTEGER, asset_name TEXT, buyer_id INTEGER, robux INTEGER, "
                    "is_pending INTEGER, location TEXT, origin TEXT NOT NULL)")
        old.execute("INSERT INTO sales VALUES ('h1', 'group:1', '2026-07-01T00:00:00.000Z', 5, "
                    "'x', 6, 7, 0, NULL, 'api')")
        old.commit()
        old.close()
        db = store.connect(path)
        cols = [r[1] for r in db.execute("PRAGMA table_info(sales)")]
        self.assertIn("price", cols)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 1)
        store.connect(path).close()                     # idempotent
        db.close()
