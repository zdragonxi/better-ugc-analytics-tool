"""Shared test fixtures. Everything here is SYNTHETIC; no test touches the network."""
from __future__ import annotations

import csv
import os
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from roblox_sales import config, store  # noqa: E402

GID_A, GID_B = 1111111, 2222222
FAKE_COOKIE = "FAKE-TEST-COOKIE-not-a-real-session"


class NoNetwork:
    """Make any real socket connection fail loudly for the duration of a test — except to
    the loopback address, where the web tests run their own server."""

    def __enter__(self):
        self._orig = orig = socket.socket.connect

        def refuse(sock, addr, *a, **k):
            if isinstance(addr, tuple) and addr and addr[0] in ("127.0.0.1", "::1"):
                return orig(sock, addr, *a, **k)
            raise AssertionError(f"a test tried to open a network connection to {addr!r}")
        socket.socket.connect = refuse
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._orig


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rsa-test-")
        self.dbpath = os.path.join(self.tmp, "sales.db")
        self.db = store.connect(self.dbpath)
        self._nonet = NoNetwork().__enter__()

    def tearDown(self):
        self._nonet.__exit__(None, None, None)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def make_cfg(tmp, extra_ini: str = "", environ=None, user=True, **over):
    ini = os.path.join(tmp, "config.ini")
    with open(ini, "w", encoding="utf-8") as f:
        f.write("[app]\ndb_path = sales.db\n"
                f"track_user_sales = {'true' if user else 'false'}\n"
                f"[group:{GID_A}]\nname = Alpha Store\n"
                f"[group:{GID_B}]\nname = Beta Store\n" + extra_ini)
    cfg = config.load(ini, environ=environ or {}, read_secret=False)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{dt.microsecond // 1000:03d}Z"


def tx(n: int, created: datetime, robux=30, asset=500, buyer=None, pending=False, name=None):
    """A synthetic economy-API Sale transaction."""
    return {"id": 0, "idHash": f"hash{n:06d}", "created": iso(created), "isPending": pending,
            "agent": {"id": buyer if buyer is not None else 70000 + n, "type": "User", "name": f"buyer{n}"},
            "details": {"id": asset, "name": name or f"Item {asset}", "type": "Asset"},
            "currency": {"amount": robux, "type": "Robux"}}


def txrow(created: datetime, robux=21, asset=500, buyer=70001, loc=2, hold="Released",
          price=None, name=None, gid=GID_A):
    """A synthetic transaction-records SaleOfGood row, in the recorded shape: amount and
    unitPrice are strings, the buyer is counterParty (id only), no transaction id."""
    return {"currencyHolder": {"type": "Group", "id": gid},
            "counterParty": {"type": "User", "id": buyer},
            "ledgerDomain": "VirtualCurrencyRobux", "ledgerReason": "SaleOfGood",
            "holdStatus": hold, "amount": str(robux),
            "details": {"productId": asset + 1, "productName": name or f"Item {asset}",
                        "productTypeId": 8, "productTargetId": asset, "quantity": 1,
                        "saleLocationTypeId": loc,
                        "unitPrice": str(price if price is not None else robux * 10 // 3),
                        "assetId": asset, "assetTypeId": 8, "collectibleItemId": None},
            "createdTime": iso(created), "updatedTime": iso(created)}


class FakeClock:
    """A clock whose sleep() advances it — for RateLimiter tests without real waiting."""

    def __init__(self, t=1000.0):
        self.t = t
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


CSV_FIELDS = ["Id", "Date and Time", "Asset Id", "Asset Name", "Asset Type", "Buyer User Id",
              "Price", "Revenue", "Hold Status", "Location", "Location Id", "Universe", "Universe Id"]


def write_csv(path, rows):
    """rows: dicts with keys id, when (datetime), asset, buyer, robux, [held], [loc]."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"Id": r["id"], "Date and Time": r["when"].strftime("%Y-%m-%d %H:%M:%S"),
                        "Asset Id": r.get("asset", 500), "Asset Name": r.get("name", "Item 500"),
                        "Asset Type": "Hat", "Buyer User Id": r.get("buyer", 1),
                        "Price": r.get("robux", 30) * 3, "Revenue": r.get("robux", 30),
                        "Hold Status": "Held" if r.get("held") else "Released",
                        "Location": r.get("loc", "Game"), "Location Id": "", "Universe": "",
                        "Universe Id": ""})
    return path


def utc(y, m, d, hh=12, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def days_ago(n, now=None):
    return (now or datetime.now(timezone.utc)) - timedelta(days=n)
