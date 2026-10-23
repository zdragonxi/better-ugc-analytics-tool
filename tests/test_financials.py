"""Financial cards and the current balance: parsing, last-known merge, persistence."""
from helpers import FAKE_COOKIE, GID_A, GID_B, TmpDirCase, make_cfg
from fakes import FakeRoblox

from roblox_sales import roblox, store, sync

NOSLEEP = lambda s: None  # noqa: E731


class FinancialTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp)
        self.s = roblox.make_session(FAKE_COOKIE)

    def serve(self, fake, a_funds=(200, {"robux": 1234}), a_sum=(200, {"pendingRobux": 567, "itemSaleRobux": 89})):
        fake.route(f"/v1/groups/{GID_A}/currency", lambda p: a_funds)
        fake.route(f"/v1/groups/{GID_A}/revenue/summary/Month", lambda p: a_sum)
        fake.json(f"/v1/groups/{GID_B}/currency", {"robux": 10})
        fake.json(f"/v1/groups/{GID_B}/revenue/summary/Month", {"pendingRobux": 20})

    def test_parse(self):
        fake = FakeRoblox(); self.serve(fake)
        with fake:
            snap = sync.refresh_financials(self.db, self.s, self.cfg, sleep=NOSLEEP)
        a = snap[f"group:{GID_A}"]
        self.assertEqual((a["funds"], a["pending"], a["sales_month"]), (1234, 567, 89))
        self.assertEqual(snap[f"group:{GID_B}"]["pending"], 20)
        self.assertEqual(store.get_json_meta(self.db, "financials"), snap)   # persisted

    def test_failed_call_keeps_last_known_value(self):
        fake = FakeRoblox(); self.serve(fake)
        with fake:
            sync.refresh_financials(self.db, self.s, self.cfg, sleep=NOSLEEP)
        fake = FakeRoblox(); self.serve(fake, a_sum=(429, {}), a_funds=(200, {"robux": 999}))
        with fake:
            snap = sync.refresh_financials(self.db, self.s, self.cfg, sleep=NOSLEEP)
        a = snap[f"group:{GID_A}"]
        self.assertEqual(a["funds"], 999)        # fresh
        self.assertEqual(a["pending"], 567)      # kept — a throttled reply is not "0 pending"

    def test_garbage_values_are_ignored(self):
        fake = FakeRoblox()
        self.serve(fake, a_funds=(200, {"robux": "lots"}), a_sum=(200, {"pendingRobux": None}))
        with fake:
            out = sync.fetch_group_financials(self.s, GID_A, sleep=NOSLEEP)
        self.assertEqual(out, {})

    def test_merge(self):
        self.assertEqual(sync.merge_financials({"funds": 1, "pending": 2}, {}), {"funds": 1, "pending": 2})
        m = sync.merge_financials({"funds": 1, "pending": 2}, {"funds": 5}, now=100)
        self.assertEqual(m, {"funds": 5, "pending": 2, "updated": 100})

    def test_balance(self):
        fake = FakeRoblox(); fake.json("/v1/user/currency", {"robux": 4321})
        with fake:
            self.assertEqual(sync.fetch_balance(self.s, sleep=NOSLEEP), 4321)
        fake = FakeRoblox(); fake.json("/v1/user/currency", {}, status=503)
        with fake:
            self.assertIsNone(sync.fetch_balance(self.s, sleep=NOSLEEP))

    def test_current_balance_is_one_value_not_a_history(self):
        self.assertIsNone(store.get_balance(self.db))
        store.set_balance(self.db, 100, now=6000)
        store.set_balance(self.db, 250, now=6100)
        self.assertEqual(store.get_balance(self.db), {"robux": 250, "ts": 6100})
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("balance_history", tables)

    def test_an_old_balance_history_table_is_left_untouched(self):
        """A database from an earlier version may still hold balance_history: nothing reads,
        writes or drops it."""
        self.db.execute("CREATE TABLE balance_history (ts INTEGER PRIMARY KEY, robux INTEGER NOT NULL)")
        self.db.execute("INSERT INTO balance_history VALUES (60, 5)")
        self.db.commit()
        self.db.close()
        self.db = store.connect(self.dbpath)                    # schema setup runs again
        store.set_balance(self.db, 7)
        self.assertEqual(self.db.execute("SELECT * FROM balance_history").fetchall(), [(60, 5)])

    def test_data_endpoint_ships_only_the_current_balance(self):
        from roblox_sales import web
        store.set_balance(self.db, 4321, now=7000)
        d = web.read_data(self.db, self.cfg, {})
        self.assertEqual(d["balance"], {"robux": 4321, "ts": 7000})
