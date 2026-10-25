# First run with a real cookie — checklist

Nothing in this repository has yet talked to Roblox. The test suite fakes every response. Do this
once, on your own machine, before you publish the repository.

1. `pip install -r requirements.txt` into a fresh venv; `python -m unittest discover -s tests` → OK.
2. `cp config.example.ini config.ini`; add ONE group you own (`[group:<id>]`, a `name`);
   set `initial_sync_days = 3` for a short first walk.
3. Put the cookie in `roblox_cookie.txt` (`chmod 600` on Linux/macOS). Confirm
   `git status` does **not** list it.
4. `python -m roblox_sales sync`. Expected: "Cookie accepted", then one line per store with pages,
   new rows and `ok`. Check:
   - the group line does **not** say `[older transactions API: …]` — that means the sales
     (transaction-records) API refused it; note the HTTP status it shows;
   - a sale you remember shows up (`python -m roblox_sales stats`);
   - the user store does not error (if it does, set `track_user_sales = false`).
5. `python -m roblox_sales` → open http://127.0.0.1:8930. Check the Pending Robux / Group Funds
   cards match the group's Transactions page on roblox.com, and the balance pill matches your
   account balance. The In-game / Website card should show a split for the synced days without
   any CSV import; compare it with the Creator Hub's sales page for the same days. A few sales
   that are still on hold there should be held here too (`SELECT created, is_pending FROM sales
   ORDER BY created DESC LIMIT 20` on `data/sales.db`).
6. Try a short backfill first: `python -m roblox_sales backfill --since <a date a week ago>`.
   It should print one line per finished day and end with `complete`. Press Ctrl+C half-way
   through once and run it again — it must resume, not restart. Then run the full
   `python -m roblox_sales backfill --restart` (reads from 2026-06-16; at 40 requests a minute,
   about 4,000 sales a minute).
7. `python -m roblox_sales catalog` (wait ~30 s after the sync). Every group should print a
   number and no `INCOMPLETE`; compare it with the number of on-sale items you see in the Creator
   Hub. If one asset type always fails, remove it from `asset_types`. If you sell classic
   clothing, try adding `2,11,12` and check whether those types answer.
8. Import one month's Creator Hub export. **This is the one format not verified against a real
   file**: if the import says "unreadable 'Date and Time' value", note the exact date format and
   extend `store.parse_ts`. Then run `verify` on the same file → "VERIFY PASSED". For a month the
   backfill also covered, `stats` should show no jump in the sale count after the import — the
   API's sales and the export's are matched, not added twice.
9. Leave it running for ~10 minutes and watch the console: no repeated 429 lines, no errors.
10. Raise `initial_sync_days` only on a fresh database (it applies to a store's first sync only).
11. After you are done: `git status` must show no `.env`, `config.ini`, cookie file or `data/`.
