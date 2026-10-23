"""SQLite storage: sales rows, CSV imports, sync watermarks, financial and balance snapshots.

Every timestamp in `sales.created` is stored in ONE canonical form,
`YYYY-MM-DDTHH:MM:SS.fffZ` (UTC), whichever source it came from. The API and the CSV export
write dates differently; normalising at insert keeps day-bucketing and the cross-source
duplicate check (a string range on `created`) correct in both directions.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    id          TEXT    PRIMARY KEY,   -- API idHash, or the CSV export's own Id
    source      TEXT    NOT NULL,      -- 'group:<id>' or 'user' — never a display name
    created     TEXT    NOT NULL,      -- canonical ISO-8601 UTC, see module docstring
    asset_id    INTEGER,
    asset_name  TEXT,
    buyer_id    INTEGER,
    robux       INTEGER,               -- the creator's cut, after Roblox's fee
    is_pending  INTEGER,               -- 1 while held (~30 days); NOT kept fresh, see README
    location    TEXT,                  -- 'Game' / 'Website' (CSV only; the API omits it)
    origin      TEXT    NOT NULL       -- 'api' or 'csv'
);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created);
CREATE INDEX IF NOT EXISTS idx_sales_nat ON sales(source, asset_id, buyer_id, created);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS csv_imports (
    source      TEXT NOT NULL,
    month       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    rows_new    INTEGER NOT NULL,
    imported_at INTEGER NOT NULL,
    PRIMARY KEY (source, month)
);
"""

DUPE_WINDOW_S = 120     # the API and CSV clocks for one sale differ by up to ~80 s
SHRINK_RATIO = 0.9      # a month re-import with < 90 % of the stored rows is refused


def connect(path: str) -> sqlite3.Connection:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")      # dashboard reads run alongside sync writes
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    return db


# ── meta (key/value) ──────────────────────────────────────────────────────────

def get_meta(db, key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key: str, value: str, commit: bool = True) -> None:
    db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    if commit:
        db.commit()


def get_json_meta(db, key: str, default=None):
    v = get_meta(db, key)
    if not v:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


def rows_epoch(db) -> str:
    """Changes whenever rows may have been deleted or rewritten (a CSV month import), so a
    dashboard holding an incremental copy knows to fetch everything again."""
    return get_meta(db, "rows_epoch") or "0"


def bump_epoch(db, commit: bool = True) -> str:
    e = str(int(time.time() * 1000))
    if e == get_meta(db, "rows_epoch"):
        e = str(int(e) + 1)
    set_meta(db, "rows_epoch", e, commit=commit)
    return e


# ── watermarks: per store, advanced only after that store's walk completed ────

def get_watermark(db, source: str) -> datetime | None:
    v = get_meta(db, f"watermark:{source}")
    return datetime.fromisoformat(v) if v else None


def set_watermark(db, source: str, dt: datetime) -> None:
    set_meta(db, f"watermark:{source}", dt.astimezone(timezone.utc).isoformat())


# ── timestamps ────────────────────────────────────────────────────────────────

def parse_ts(s: str) -> datetime | None:
    """Parse an API ('2026-07-01T18:22:05.123Z') or CSV ('2026-07-01 18:22:05', optional
    ' UTC' / zone) timestamp to an aware UTC datetime. A value with no zone is UTC — that
    is what the export uses. None for anything unparseable."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if t.upper().endswith(" UTC"):
        t = t[:-4]
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1] + "+00:00"
    t = t.replace(" ", "T", 1)
    # Python 3.10's fromisoformat accepts only 3 or 6 fractional digits
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", t)
    if m:
        t = f"{m.group(1)}.{(m.group(2) + '000000')[:6]}{m.group(3)}"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canon(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{dt.microsecond // 1000:03d}Z"


# ── upsert + cross-source duplicate detection ────────────────────────────────

def find_natural_dupe(db, source: str, asset_id, buyer_id, created: str, robux):
    """The CSV export and the API give the SAME sale different ids, and their timestamps
    can differ by up to ~80 s, so an id check alone double-counts any period both sources
    cover. A row is the same sale when source, asset, buyer and robux all match within
    DUPE_WINDOW_S. The robux amount is essential: rentals let one buyer pay for one item
    several times within seconds (small rent payments, then the full price) — those are
    real, separate revenue, and they always differ in amount."""
    if not (asset_id and buyer_id and created):
        return None
    t = parse_ts(created)
    if t is None:
        return None
    lo = canon(t - timedelta(seconds=DUPE_WINDOW_S))
    hi = canon(t + timedelta(seconds=DUPE_WINDOW_S))
    row = db.execute(
        "SELECT id FROM sales WHERE source=? AND asset_id=? AND buyer_id=? AND robux IS ? "
        "AND created BETWEEN ? AND ? LIMIT 1",
        (source, asset_id, buyer_id, robux, lo, hi)).fetchone()
    return row[0] if row else None


def upsert_api_tx(db, tx: dict, source: str) -> bool:
    """Insert or refresh one API transaction. True when it was a new sale."""
    details = tx.get("details") or {}
    agent = tx.get("agent") or {}
    currency = tx.get("currency") or {}
    # the numeric `id` is 0 on modern transactions — idHash is the real key
    tx_id = tx.get("idHash") or tx.get("purchaseToken")
    if not tx_id:
        if tx.get("id"):
            tx_id = f"id:{tx['id']}"
        else:
            return False                        # nothing stable to key it on — skip
    dt = parse_ts(tx.get("created", ""))
    if dt is None:
        return False
    created = canon(dt)
    pending = 1 if tx.get("isPending") else 0
    robux = currency.get("amount")
    if db.execute("SELECT 1 FROM sales WHERE id=?", (tx_id,)).fetchone() is None:
        dupe = find_natural_dupe(db, source, details.get("id"), agent.get("id"), created, robux)
        if dupe:            # the same sale, already stored from a CSV import
            db.execute("UPDATE sales SET is_pending=? WHERE id=?", (pending, dupe))
            return False
        new = True
    else:
        new = False
    db.execute(
        "INSERT INTO sales (id, source, created, asset_id, asset_name, buyer_id, robux, "
        "is_pending, location, origin) VALUES (?,?,?,?,?,?,?,?,NULL,'api') "
        "ON CONFLICT(id) DO UPDATE SET is_pending=excluded.is_pending, robux=excluded.robux",
        (tx_id, source, created, details.get("id"), details.get("name"), agent.get("id"),
         robux, pending))
    return new


# ── CSV import (Creator Hub monthly sales export) ────────────────────────────

CSV_NAME_RE = re.compile(r"^Sales_Group(\d+)_(\d{4}-\d{2})\.csv(\.zip)?$", re.IGNORECASE)


class ImportRefused(ValueError):
    """The file was not imported and nothing was changed."""


def check_csv_name(fname: str, known_group_ids) -> tuple[int, str]:
    """(group_id, 'YYYY-MM') for an acceptable export filename, else ImportRefused.

    The filename is the only place the export states its store and month, and a month
    import REPLACES that month — so the name is checked strictly: a browser's ' (1)' suffix
    or a store this dashboard is not configured for is refused rather than guessed at
    (an unknown store would otherwise merge into the "All stores" totals)."""
    base = os.path.basename(fname or "")
    m = CSV_NAME_RE.match(base)
    if not m:
        raise ImportRefused("filename must be exactly Sales_Group<id>_<YYYY-MM>.csv or .csv.zip "
                            "(remove any ' (1)' your browser added)")
    gid = int(m.group(1))
    if gid not in set(known_group_ids):
        raise ImportRefused(f"that export is for group {gid}, which is not one of the groups "
                            f"in your config — add it there first")
    return gid, m.group(2)


def _csv_rows(path: str):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    with z.open(name) as f:
                        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            yield from csv.DictReader(f)


def _int_or_none(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def import_csv(db, path: str, known_group_ids, force: bool = False,
               display_name: str | None = None) -> dict:
    """Import one monthly export, REPLACING that month's rows for that store.

    The export is the authoritative record of a finished month (it is complete where the
    API's pagination stops, its hold status is final, and it carries Location), so the
    month is wiped and rewritten in one transaction. Guard: a file with materially fewer
    rows than are already stored for that month is refused — a truncated download would
    otherwise destroy sales that the API can no longer page back to. `force=True` is the
    deliberate override for a shrink you know is real."""
    fname = os.path.basename(path)
    gid, month = check_csv_name(fname, known_group_ids)
    source = f"group:{gid}"
    rows = [r for r in _csv_rows(path) if (r.get("Id") or "").strip()]
    if not rows:
        raise ImportRefused(f"{fname}: no rows — nothing imported, nothing removed")

    parsed = []
    outside = 0
    for r in rows:
        dt = parse_ts(r.get("Date and Time", ""))
        if dt is None:
            raise ImportRefused(f"{fname}: unreadable 'Date and Time' value — nothing imported")
        if canon(dt)[:7] != month:
            outside += 1    # boundary sales: kept, and deduplicated like any other row
        parsed.append((r, canon(dt)))

    lo, hi = f"{month}-01T00:00:00.000Z", f"{month}-31T23:59:59.999Z"
    have = db.execute("SELECT COUNT(*) FROM sales WHERE source=? AND created BETWEEN ? AND ?",
                      (source, lo, hi)).fetchone()[0]
    if have and len(parsed) < have * SHRINK_RATIO and not force:
        raise ImportRefused(
            f"{fname} has {len(parsed):,} rows but {have:,} are already stored for {month} — "
            f"this looks like a truncated or partial export. Download the full month again, "
            f"or re-run with --force if the shrink is real.")

    new = 0
    if db.in_transaction:
        db.commit()
    try:
        db.execute("BEGIN")
        wiped = db.execute("DELETE FROM sales WHERE source=? AND created BETWEEN ? AND ?",
                           (source, lo, hi)).rowcount
        for r, created in parsed:
            tx_id = r["Id"].strip()
            aid = _int_or_none(r.get("Asset Id"))
            bid = _int_or_none(r.get("Buyer User Id"))
            rbx = _int_or_none(r.get("Revenue"))
            pending = 1 if (r.get("Hold Status") or "").strip().lower() == "held" else 0
            loc = (r.get("Location") or "").strip() or None
            if db.execute("SELECT 1 FROM sales WHERE id=?", (tx_id,)).fetchone() is None:
                # duplicates inside the file itself (or a neighbour-month API row at the
                # boundary) are the same sale under another id
                dupe = find_natural_dupe(db, source, aid, bid, created, rbx)
                if dupe:
                    db.execute("UPDATE sales SET is_pending=?, location=COALESCE(?, location) "
                               "WHERE id=?", (pending, loc, dupe))
                    continue
                new += 1
            db.execute(
                "INSERT INTO sales (id, source, created, asset_id, asset_name, buyer_id, robux, "
                "is_pending, location, origin) VALUES (?,?,?,?,?,?,?,?,?,'csv') "
                "ON CONFLICT(id) DO UPDATE SET is_pending=excluded.is_pending, "
                "location=excluded.location",
                (tx_id, source, created, aid, (r.get("Asset Name") or "").strip() or None, bid,
                 rbx, pending, loc))
        stored = db.execute("SELECT COUNT(*) FROM sales WHERE source=? AND created BETWEEN ? "
                            "AND ?", (source, lo, hi)).fetchone()[0]
        db.execute(
            "INSERT INTO csv_imports (source, month, filename, rows, rows_new, imported_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(source, month) DO UPDATE SET "
            "filename=excluded.filename, rows=excluded.rows, rows_new=excluded.rows_new, "
            "imported_at=excluded.imported_at",
            (source, month, fname, stored, max(0, stored - have), int(time.time())))
        bump_epoch(db, commit=False)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {"filename": fname, "source": source, "name": display_name or source,
            "month": month, "rows": stored, "replaced": wiped, "rows_new": max(0, stored - have),
            "outside_month": outside}


def verify_csv(db, path: str, known_group_ids) -> dict:
    """Compare an export with the database without changing anything: which of the file's
    sales are missing from the database (matched by id or as the same natural sale)."""
    fname = os.path.basename(path)
    gid, month = check_csv_name(fname, known_group_ids)
    source = f"group:{gid}"
    missing = []
    total = 0
    for r in _csv_rows(path):
        if not (r.get("Id") or "").strip():
            continue
        total += 1
        dt = parse_ts(r.get("Date and Time", ""))
        created = canon(dt) if dt else ""
        if db.execute("SELECT 1 FROM sales WHERE id=?", (r["Id"].strip(),)).fetchone():
            continue
        if find_natural_dupe(db, source, _int_or_none(r.get("Asset Id")),
                             _int_or_none(r.get("Buyer User Id")), created,
                             _int_or_none(r.get("Revenue"))):
            continue
        missing.append({"date": created, "item": r.get("Asset Name"), "robux": r.get("Revenue")})
    return {"filename": fname, "month": month, "rows": total, "missing": missing}


def imports_list(db) -> list[dict]:
    return [{"source": s, "month": m, "filename": f, "rows": n, "rows_new": nn, "imported_at": t}
            for s, m, f, n, nn, t in db.execute(
                "SELECT source, month, filename, rows, rows_new, imported_at FROM csv_imports "
                "ORDER BY month DESC, source")]


# ── current balance (one value, no history) ──────────────────────────────────

def set_balance(db, robux: int, now: float | None = None) -> None:
    """Remember the latest balance so the top-bar pill shows it right after a restart."""
    set_meta(db, "balance", json.dumps({"robux": int(robux),
                                        "ts": int(now if now is not None else time.time())}))


def get_balance(db) -> dict | None:
    """{"robux": int, "ts": epoch} or None if no balance was ever read."""
    b = get_json_meta(db, "balance")
    return b if isinstance(b, dict) and isinstance(b.get("robux"), int) else None
