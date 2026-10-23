"""Items On Sale: by-creator walks, the complete-or-keep rule, persistence."""
import json

from helpers import FAKE_COOKIE, GID_A, GID_B, TmpDirCase, make_cfg
from fakes import FakeRoblox

from roblox_sales import catalog, roblox, store

NOSLEEP = lambda s: None  # noqa: E731
PATH = "/v1/items/by-creator"


def item(n, on_sale):
    return {"id": 900000 + n, "name": f"Item {n}", "price": 50 if on_sale else None,
            "saleStatus": 0 if on_sale else 1, "moderationStatus": 3, "createdTime": "2026-01-01T00:00:00Z"}


class Catalog:
    """A fake by-creator: {(group, type): [items]} served in pages of 30 with a cursor chain;
    `fail` maps (group, type) to a status code that type always answers with."""

    def __init__(self, data, fail=None, page=30):
        self.data, self.fail, self.page = data, fail or {}, page
        self.seen = []

    def __call__(self, params):
        key = (int(params["groupId"]), int(params["assetType"]))
        self.seen.append((key, params.get("cursor")))
        if key in self.fail:
            return self.fail[key], {"errors": [{"message": "nope"}]}
        rows = self.data.get(key, [])
        start = int(params.get("cursor") or 0)
        chunk = rows[start:start + self.page]
        nxt = str(start + self.page) if start + self.page < len(rows) else None
        return 200, {"items": chunk, "nextCursor": nxt}


class CatalogTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp, asset_types=[8, 41])
        self.s = roblox.make_session(FAKE_COOKIE)

    def refresh(self, fake_catalog):
        fake = FakeRoblox()
        fake.route(PATH, fake_catalog)
        with fake:
            snap = catalog.refresh_all(self.db, self.s, self.cfg, sleep=NOSLEEP)
        self.calls = fake.calls
        return snap

    def test_multi_page_walk_follows_the_cursor_chain(self):
        hats = [item(i, i % 3 != 0) for i in range(95)]          # 4 pages, 63 on sale
        hair = [item(1000 + i, True) for i in range(7)]
        fc = Catalog({(GID_A, 8): hats, (GID_A, 41): hair})
        snap = self.refresh(fc)
        a = snap[f"group:{GID_A}"]
        self.assertEqual(a["items"], 63 + 7)
        self.assertEqual(a["types"], {"8": 63, "41": 7})
        self.assertIsNone(a["error"])
        self.assertEqual([c for k, c in fc.seen if k == (GID_A, 8)], [None, "30", "60", "90"])
        self.assertEqual(snap[f"group:{GID_B}"]["items"], 0)      # an empty group is a real 0
        self.assertEqual(store.get_json_meta(self.db, "catalog"), snap)

    def test_a_failing_type_keeps_the_last_complete_count(self):
        self.refresh(Catalog({(GID_A, 8): [item(i, True) for i in range(40)]}))
        before = store.get_json_meta(self.db, "catalog")[f"group:{GID_A}"]
        self.assertEqual(before["items"], 40)
        # now the hats would be 41, but the hair type answers 500 every time
        snap = self.refresh(Catalog({(GID_A, 8): [item(i, True) for i in range(41)]},
                                    fail={(GID_A, 41): 500}))
        a = snap[f"group:{GID_A}"]
        self.assertEqual((a["items"], a["as_of"]), (40, before["as_of"]))
        self.assertIn("type 41", a["error"])
        self.assertEqual(snap[f"group:{GID_B}"]["items"], 0)      # the other group still updates

    def test_first_refresh_failure_is_unknown_not_zero(self):
        snap = self.refresh(Catalog({}, fail={(GID_A, 8): 403}))
        a = snap[f"group:{GID_A}"]
        self.assertIsNone(a["items"])
        self.assertIsNone(a["as_of"])
        self.assertTrue(a["error"])

    def test_rate_limited_type_never_produces_a_partial_count(self):
        """The trap this module exists for: half the types answer, half are throttled."""
        fc = Catalog({(GID_A, 8): [item(i, True) for i in range(20)]}, fail={(GID_A, 41): 429})
        snap = self.refresh(fc)
        self.assertIsNone(snap[f"group:{GID_A}"]["items"])

    def test_empty_page_mid_list_is_unknown(self):
        def answer(params):
            if params.get("cursor"):
                return 200, {"items": [], "nextCursor": None}
            return 200, {"items": [item(1, True)], "nextCursor": "next"}
        snap = self.refresh(answer)
        self.assertIsNone(snap[f"group:{GID_A}"]["items"])

    def test_everything_empty_after_a_real_count_is_not_believed(self):
        self.refresh(Catalog({(GID_A, 8): [item(i, True) for i in range(12)]}))
        snap = self.refresh(Catalog({}))
        self.assertEqual(snap[f"group:{GID_A}"]["items"], 12)
        self.assertTrue(snap[f"group:{GID_A}"]["error"])

    def test_on_sale_means_price_is_not_null(self):
        rows = [item(1, True), item(2, False), dict(item(3, True), price=0), {"id": 4}]
        snap = self.refresh(Catalog({(GID_A, 8): rows}))
        self.assertEqual(snap[f"group:{GID_A}"]["types"]["8"], 2)  # price 0 still counts

    def test_only_by_creator_gets(self):
        self.refresh(Catalog({(GID_A, 8): [item(i, True) for i in range(31)]}))
        self.assertTrue(self.calls)
        for method, url, _ in self.calls:
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith("https://itemconfiguration.roblox.com/v1/items/by-creator?"), url)
            self.assertIn("limit=30", url)

    def test_record_shape_served_to_the_page(self):
        from roblox_sales import web
        self.refresh(Catalog({(GID_A, 8): [item(1, True)]}))
        d = web.read_data(self.db, self.cfg, {})
        self.assertEqual(set(d["catalog"][f"group:{GID_A}"]), {"items", "as_of", "attempt", "error"})
        json.dumps(d)
