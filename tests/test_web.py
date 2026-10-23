"""The dashboard server: rendering without a cookie, incremental rows, host and origin
checks, the optional password, and CSV upload."""
import http.client
import json
import os
import threading
from urllib.parse import quote

from helpers import GID_A, TmpDirCase, make_cfg, utc, write_csv

from roblox_sales import auth, demo, store, web


class WebCase(TmpDirCase):
    password = ""

    def setUp(self):
        super().setUp()
        self.db.close()                      # demo.build replaces the file
        demo.build(self.dbpath, seed=3)
        self.db = store.connect(self.dbpath)
        self.cfg = make_cfg(self.tmp, port=0, password_hash=self.password and auth.hash_password(self.password, 1000))
        self.httpd = web.serve(self.cfg)
        self.port = self.cfg.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data


class DashboardTests(WebCase):
    def test_page_and_assets_render_without_a_cookie(self):
        r, body = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"Sales Analytics", body)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        for f in ("app.js", "style.css", "chart-4.4.1.umd.js", "favicon.svg"):
            self.assertEqual(self.req("GET", "/static/" + f)[0].status, 200, f)
        self.assertEqual(self.req("GET", "/static/../config.py")[0].status, 404)

    def test_data_full_then_incremental(self):
        r, body = self.req("GET", "/data")
        d = json.loads(body)
        self.assertEqual(r.status, 200)
        self.assertEqual(len(d["rows"]), d["count"])
        self.assertIn("catalog", d)
        self.assertEqual(d["status"]["polling"], False)
        q = f"/data?epoch={d['epoch']}&rowid={d['max_rowid']}&count={d['count']}"
        d2 = json.loads(self.req("GET", q)[1])
        self.assertNotIn("rows", d2)
        self.assertEqual(d2["rows_add"], [])
        # a new sale arrives → only it is sent
        self.db.execute("INSERT INTO sales (id, source, created, robux, origin) "
                        "VALUES ('new1', ?, '2026-01-01T00:00:00.000Z', 5, 'api')", (f"group:{GID_A}",))
        self.db.commit()
        d3 = json.loads(self.req("GET", q)[1])
        self.assertEqual([r[0] for r in d3["rows_add"]], [d["max_rowid"] + 1])
        # a deletion below the client's base → full reload
        self.db.execute("DELETE FROM sales WHERE rowid = 1")
        self.db.commit()
        d4 = json.loads(self.req("GET", q)[1])
        self.assertIn("rows", d4)
        # a stale epoch → full reload
        d5 = json.loads(self.req("GET", f"/data?epoch=old&rowid={d['max_rowid']}&count={d['count']}")[1])
        self.assertIn("rows", d5)

    def test_unknown_host_header_is_refused(self):
        self.assertEqual(self.req("GET", "/data", host="attacker.example")[0].status, 421)

    def test_writes_need_the_custom_header_and_same_origin(self):
        p = write_csv(os.path.join(self.tmp, "x.csv"), [dict(id="w1", when=utc(2025, 2, 3))])
        with open(p, "rb") as f:
            body = f.read()
        name = f"Sales_Group{GID_A}_2025-02.csv"
        r, _ = self.req("POST", "/import-csv", body, {"X-Filename": name})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/import-csv", body, {"X-Filename": name, "X-RSA-Request": "1",
                                                       "Origin": "https://attacker.example"})
        self.assertEqual(r.status, 403)
        r, out = self.req("POST", "/import-csv", body, {"X-Filename": quote(name), "X-RSA-Request": "1",
                                                         "Origin": f"http://127.0.0.1:{self.port}"})
        j = json.loads(out)
        self.assertTrue(j["ok"], j)
        self.assertEqual((j["month"], j["rows"]), ("2025-02", 1))

    def test_upload_for_an_unconfigured_group_is_refused(self):
        r, out = self.req("POST", "/import-csv", b"Id\n1\n",
                          {"X-Filename": "Sales_Group9999999_2025-02.csv", "X-RSA-Request": "1"})
        self.assertFalse(json.loads(out)["ok"])

    def test_no_login_page_without_a_password(self):
        r, _ = self.req("GET", "/login")
        self.assertEqual((r.status, r.getheader("Location")), (303, "/"))


class PasswordTests(WebCase):
    password = "correct horse battery"

    def test_password_flow(self):
        r, _ = self.req("GET", "/")
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))
        self.assertEqual(self.req("GET", "/data")[0].status, 401)
        r, _ = self.req("POST", "/login", "password=wrong",
                        {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.getheader("Location"), "/login?e=1")
        r, _ = self.req("POST", "/login", "password=" + quote(self.password),
                        {"Content-Type": "application/x-www-form-urlencoded"})
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        tok = cookie.split(";")[0]
        self.assertEqual(self.req("GET", "/data", headers={"Cookie": tok})[0].status, 200)

    def test_login_is_throttled(self):
        for _ in range(auth.MAX_FAILS):
            self.req("POST", "/login", "password=nope", {"Content-Type": "application/x-www-form-urlencoded"})
        r, _ = self.req("POST", "/login", "password=" + quote(self.password),
                        {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertIsNone(r.getheader("Set-Cookie"))      # even the right password waits


class AuthUnitTests(TmpDirCase):
    def test_hash_roundtrip(self):
        h = auth.hash_password("s3cret-pass", 1000)
        self.assertTrue(auth.verify_password("s3cret-pass", h))
        self.assertFalse(auth.verify_password("other", h))
        self.assertFalse(auth.verify_password("x", "garbage"))
