"""The safety contract: this app only ever sends HTTPS GETs to *.roblox.com, never touches a
logout or token endpoint, and only sends the cookie to Roblox."""
import ast
import os
import re
import threading
import time
from datetime import timedelta

from helpers import FAKE_COOKIE, GID_A, GID_B, ROOT, TmpDirCase, days_ago, make_cfg, tx
from fakes import FakeRoblox, pages

from roblox_sales import poller as poller_mod, roblox, store

PKG = os.path.join(ROOT, "roblox_sales")
FORBIDDEN_URL = re.compile(r"logout|csrf|xsrf|validate-machine|token|signup|login", re.I)


def py_sources():
    for dirpath, _, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


class GuardTests(TmpDirCase):
    def test_non_get_methods_are_refused_before_sending(self):
        s = roblox.make_session(FAKE_COOKIE)
        fake = FakeRoblox()
        with fake:
            for m in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
                with self.assertRaises(roblox.ReadOnlyViolation, msg=m):
                    s.request(m, "https://economy.roblox.com/v1/user/currency")
            with self.assertRaises(roblox.ReadOnlyViolation):
                s.post("https://auth.roblox.com/v2/logout")
        self.assertEqual(fake.calls, [])            # nothing reached the transport

    def test_only_https_roblox_hosts(self):
        s = roblox.make_session(FAKE_COOKIE)
        for url in ("http://economy.roblox.com/x", "https://example.com/",
                    "https://roblox.com.example.net/", "https://evilroblox.com/",
                    "https://economy.roblox.com.evil.io/"):
            with self.assertRaises(roblox.ReadOnlyViolation, msg=url):
                s.get(url)
        roblox.check_request("GET", "https://economy.roblox.com/v1/user/currency")

    def test_redirect_off_roblox_is_refused(self):
        """send() is guarded too, so a redirect hop cannot leave *.roblox.com."""
        import requests
        s = roblox.make_session(FAKE_COOKIE)
        prep = requests.Request("GET", "https://example.com/elsewhere").prepare()
        with self.assertRaises(roblox.ReadOnlyViolation):
            s.send(prep)

    def test_cookie_is_scoped_to_roblox(self):
        s = roblox.make_session(FAKE_COOKIE)
        (c,) = list(s.cookies)
        self.assertEqual((c.name, c.domain), (".ROBLOSECURITY", ".roblox.com"))
        self.assertNotIn(FAKE_COOKIE, repr(s))


class FullCycleTests(TmpDirCase):
    def test_a_full_poller_cycle_only_issues_safe_gets(self):
        cfg = make_cfg(self.tmp)
        cfg._cookie = FAKE_COOKIE
        cfg.proxy = ""
        fake = FakeRoblox()
        fake.json("/v1/users/authenticated", {"id": 4242, "name": "someone"})
        txs = [tx(i, days_ago(0.1) - timedelta(hours=i)) for i in range(150)]
        fake.route(f"/v2/groups/{GID_A}/transactions", pages(txs))
        fake.route(f"/v2/groups/{GID_B}/transactions", pages([]))
        fake.route("/v2/users/4242/transactions", pages([]))
        for g in (GID_A, GID_B):
            fake.json(f"/v1/groups/{g}/currency", {"robux": 1})
            fake.json(f"/v1/groups/{g}/revenue/summary/Month", {"pendingRobux": 2})
        fake.json("/v1/user/currency", {"robux": 3})
        fake.json("/v1/items/by-creator", {"items": [{"id": 1, "price": 10}], "nextCursor": None})
        cfg.asset_types = [8, 41]
        spacing = (poller_mod.sync.PAGE_SLEEP, poller_mod.sync.STORE_SPACING, poller_mod.sync.FIN_SPACING,
                   poller_mod.BURST_GAP, poller_mod.catalog.PAGE_SLEEP)
        poller_mod.sync.PAGE_SLEEP = poller_mod.sync.STORE_SPACING = poller_mod.sync.FIN_SPACING = 0
        poller_mod.BURST_GAP = poller_mod.catalog.PAGE_SLEEP = 0
        try:
            with fake:
                p = poller_mod.Poller(cfg, log=lambda *_: None)
                p.start()
                deadline = time.time() + 20
                while time.time() < deadline and not (
                        p.state.get("last_sync") and store.get_json_meta(self.db, "catalog")):
                    time.sleep(0.05)
                p.stop()
                p._thread.join(10)
        finally:
            (poller_mod.sync.PAGE_SLEEP, poller_mod.sync.STORE_SPACING, poller_mod.sync.FIN_SPACING,
             poller_mod.BURST_GAP, poller_mod.catalog.PAGE_SLEEP) = spacing
        self.assertTrue(p.state.get("last_sync"), p.state)
        self.assertGreater(len(fake.calls), 5)
        for method, url, headers in fake.calls:
            self.assertEqual(method, "GET", url)
            self.assertRegex(url, r"^https://[a-z]+\.roblox\.com/")
            self.assertIsNone(FORBIDDEN_URL.search(url.split("?")[0]), url)
            self.assertNotIn("x-csrf-token", {k.lower() for k in headers})
        n = self.db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        self.assertEqual(n, 150)
        self.assertTrue(any("/v1/items/by-creator" in u for _, u, _ in fake.calls))   # catalog ran
        cat = store.get_json_meta(self.db, "catalog")
        self.assertEqual(cat[f"group:{GID_A}"]["items"], 2)       # 1 per type × 2 types


class SourceScanTests(TmpDirCase):
    """Static checks over the package source, so a future edit cannot quietly add a
    write call or a token/logout endpoint."""

    def test_no_write_method_calls_in_the_package(self):
        bad = []
        for path in py_sources():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("post", "put", "patch", "delete", "options"):
                    bad.append(f"{os.path.basename(path)}:{node.lineno} .{node.func.attr}()")
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "method":
                            bad.append(f"{os.path.basename(path)}:{node.lineno} method=")
        self.assertEqual(bad, [])

    def test_no_logout_or_token_endpoints_anywhere(self):
        hits = []
        for path in py_sources():
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if re.search(r"logout|csrf|xsrf|validate-machine", line, re.I):
                        hits.append(f"{os.path.basename(path)}:{i}: {line.strip()}")
        self.assertEqual(hits, [])

    def test_every_roblox_url_constant_is_https_and_read_only_looking(self):
        urls = [v for k, v in vars(roblox).items() if k.endswith("_URL")]
        self.assertGreaterEqual(len(urls), 6)
        for u in urls:
            roblox.check_request("GET", u.format(id=1))
            self.assertIsNone(FORBIDDEN_URL.search(u), u)

    def test_the_scanner_itself_catches_a_violation(self):
        """Negative control: the AST check must flag a planted .post() call."""
        tree = ast.parse("import requests\nrequests.Session().post('https://x.roblox.com/')")
        found = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "post"]
        self.assertEqual(len(found), 1)
