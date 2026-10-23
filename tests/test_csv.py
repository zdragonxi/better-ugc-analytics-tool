"""Creator Hub CSV import: month replace, shrink guard, filename rules, cross-source dedupe."""
import os
import zipfile
from datetime import timedelta

from helpers import GID_A, GID_B, TmpDirCase, tx, utc, write_csv

from roblox_sales import store

KNOWN = [GID_A, GID_B]
SRC = f"group:{GID_A}"


def month_rows(n, y=2026, m=3, start_id=0, **kw):
    return [dict(id=f"csv{start_id + i:05d}", when=utc(y, m, 1 + i % 27, 10, i % 60),
                 buyer=1000 + start_id + i, **kw) for i in range(n)]


class CsvTests(TmpDirCase):
    def path(self, name):
        return os.path.join(self.tmp, name)

    def count(self, like="2026-03"):
        return self.db.execute("SELECT COUNT(*) FROM sales WHERE source=? AND created LIKE ?",
                               (SRC, like + "%")).fetchone()[0]

    def test_import_and_reimport_replaces_the_month(self):
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-03.csv"), month_rows(50))
        r = store.import_csv(self.db, p, KNOWN)
        self.assertEqual((r["rows"], r["month"]), (50, "2026-03"))
        self.assertEqual(self.count(), 50)
        # the month gets corrected: one sale fewer, one changed — re-import replaces it
        rows = month_rows(49)
        write_csv(p, rows)
        store.import_csv(self.db, p, KNOWN)
        self.assertEqual(self.count(), 49)
        self.assertEqual(len(store.imports_list(self.db)), 1)

    def test_location_and_canonical_dates(self):
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-03.csv"),
                      [dict(id="x1", when=utc(2026, 3, 5, 7, 8, 9), loc="Website", held=True)])
        store.import_csv(self.db, p, KNOWN)
        created, loc, pend, origin = self.db.execute(
            "SELECT created, location, is_pending, origin FROM sales").fetchone()
        self.assertEqual(created, "2026-03-05T07:08:09.000Z")
        self.assertEqual((loc, pend, origin), ("Website", 1, "csv"))

    def test_truncated_export_is_refused_and_changes_nothing(self):
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-03.csv"), month_rows(100))
        store.import_csv(self.db, p, KNOWN)
        epoch = store.rows_epoch(self.db)
        write_csv(p, month_rows(60))
        with self.assertRaises(store.ImportRefused):
            store.import_csv(self.db, p, KNOWN)
        self.assertEqual(self.count(), 100)
        self.assertEqual(store.rows_epoch(self.db), epoch)
        r = store.import_csv(self.db, p, KNOWN, force=True)     # the deliberate override
        self.assertEqual(r["rows"], 60)

    def test_unconfigured_group_is_refused(self):
        p = write_csv(self.path("Sales_Group9999999_2026-03.csv"), month_rows(3))
        with self.assertRaises(store.ImportRefused):
            store.import_csv(self.db, p, KNOWN)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 0)

    def test_browser_duplicate_suffix_and_bad_names_are_refused(self):
        for name in (f"Sales_Group{GID_A}_2026-03 (1).csv", f"Sales_Group{GID_A}.csv",
                     "sales.csv", f"Group{GID_A}_2026-03.csv", f"Sales_Group{GID_A}_2026-3.csv"):
            with self.assertRaises(store.ImportRefused, msg=name):
                store.check_csv_name(name, KNOWN)
        self.assertEqual(store.check_csv_name(f"Sales_Group{GID_A}_2026-03.csv.zip", KNOWN),
                         (GID_A, "2026-03"))

    def test_zip_export(self):
        csvp = write_csv(self.path("inner.csv"), month_rows(12))
        zp = self.path(f"Sales_Group{GID_A}_2026-03.csv.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.write(csvp, f"Sales_Group{GID_A}_2026-03.csv")
        self.assertEqual(store.import_csv(self.db, zp, KNOWN)["rows"], 12)

    def test_empty_file_is_refused(self):
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-03.csv"), [])
        with self.assertRaises(store.ImportRefused):
            store.import_csv(self.db, p, KNOWN)

    def test_api_row_and_csv_row_for_the_same_sale_count_once(self):
        """Same sale, different ids, clocks 80 s apart: stored once, either order."""
        when = utc(2026, 4, 10, 15, 0, 0)
        # API first, then the export
        store.upsert_api_tx(self.db, tx(1, when, robux=30, buyer=77, asset=500), SRC)
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-04.csv"),
                      [dict(id="csvA", when=when + timedelta(seconds=80), buyer=77, robux=30)])
        store.import_csv(self.db, p, KNOWN)
        self.assertEqual(self.count("2026-04"), 1)
        # export first, then the API reports it again (e.g. inside the sync overlap)
        store.upsert_api_tx(self.db, tx(2, when + timedelta(seconds=5), robux=30, buyer=77, asset=500),
                            SRC)
        self.assertEqual(self.count("2026-04"), 1)

    def test_rental_payments_are_not_merged(self):
        when = utc(2026, 4, 10, 15, 0, 0)
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-04.csv"),
                      [dict(id="r1", when=when, buyer=77, robux=3),
                       dict(id="r2", when=when + timedelta(seconds=30), buyer=77, robux=30)])
        store.import_csv(self.db, p, KNOWN)
        self.assertEqual(self.count("2026-04"), 2)

    def test_verify_reports_missing_rows_without_changing_anything(self):
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-03.csv"), month_rows(10))
        store.import_csv(self.db, p, KNOWN)
        write_csv(p, month_rows(12))
        r = store.verify_csv(self.db, p, KNOWN)
        self.assertEqual((r["rows"], len(r["missing"])), (12, 2))
        self.assertEqual(self.count(), 10)

    def test_import_bumps_the_rows_epoch(self):
        e0 = store.rows_epoch(self.db)
        p = write_csv(self.path(f"Sales_Group{GID_A}_2026-03.csv"), month_rows(3))
        store.import_csv(self.db, p, KNOWN)
        self.assertNotEqual(store.rows_epoch(self.db), e0)

    def test_timestamp_parsing(self):
        for s, want in [("2026-07-01T18:22:05.123Z", "2026-07-01T18:22:05.123Z"),
                        ("2026-07-01T18:22:05.1234567Z", "2026-07-01T18:22:05.123Z"),
                        ("2026-07-01 18:22:05", "2026-07-01T18:22:05.000Z"),
                        ("2026-07-01 18:22:05 UTC", "2026-07-01T18:22:05.000Z"),
                        ("2026-07-01T20:22:05+02:00", "2026-07-01T18:22:05.000Z")]:
            self.assertEqual(store.canon(store.parse_ts(s)), want, s)
        self.assertIsNone(store.parse_ts("yesterday"))
