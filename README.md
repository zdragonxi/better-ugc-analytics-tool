# Roblox Sales Analytics

A self-hosted, **read-only** sales dashboard for Roblox UGC creators. It keeps your own copy of
your group (and account) sales in a local SQLite database and shows them the way you actually
want to look at them: per-day charts with the top items broken out, an items table, group funds
and pending Robux, items on sale, a "golden ratio", your current Robux balance, the in-game vs
website split, and Creator Hub CSV imports for months the API can no longer reach.

One Python process, one dependency (`requests`), no account on any third-party service.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported by Roblox
> Corporation. "Roblox" is a trademark of Roblox Corporation. Using your account with any tool is
> your responsibility under the [Roblox Terms of Use](https://en.help.roblox.com/hc/en-us/articles/115004647846).

![Dashboard with synthetic demo data](docs/dashboard.png)

*All numbers, stores and items in the screenshot are synthetic (`python -m roblox_sales demo`).*

## What it shows

| Panel | Source |
|---|---|
| Robux earned, sales, avg/day, avg/sale, per-day chart, items table, all-time-high marker | your synced sales rows |
| Store tabs, **Compare** (each store aligned to its own first sale), drag-to-select date range | your synced sales rows |
| In-game vs website split | group sales from the sales API (see below), and CSV imports |
| Pending Robux, group funds | Roblox's group revenue summary / currency endpoints, every 2 min |
| Items On Sale | a read-only count of each group's catalog, every 10 min (see below) |
| Golden Ratio | (pending Robux ÷ items on sale) ÷ average Robux per sale in the selected range |
| Robux balance pill (top bar) | your account's current balance, read every 2 min (no history is kept) |
| Import monthly CSV, imported months | Creator Hub monthly sales exports |
| **Hide stats** | blurs every number for screen-sharing; graphs stay visible |

## Quick start

Requires Python 3.10 or newer.

```bash
git clone https://github.com/zdragonxi/better-ugc-analytics-tool.git
cd better-ugc-analytics-tool
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Try it with synthetic data first — no cookie, no network:

```bash
python -m roblox_sales demo --serve
```

Open <http://127.0.0.1:8930>. Then set it up for real:

```bash
cp config.example.ini config.ini      # edit: add a [group:<id>] section per group
python -m roblox_sales sync           # one sync, prints a per-store summary
python -m roblox_sales                # dashboard + background sync
```

## Configure

Everything lives in `config.ini` (copy `config.example.ini`; every key is commented). Each key
can also be set by an `RSA_*` environment variable or a `.env` file next to `config.ini` (see
`.env.example`); the environment wins.

- **Groups** — one `[group:<id>]` section per group: `name` (display only — renaming never
  touches stored data) and `combine` (include in "All stores"; set `false` for a store whose
  money you account for separately).
- **Your own account's sales** — `track_user_sales = true` (default).
- **Proxy** (optional) — `proxy = http://user:pass@host:port`. Roblox may throttle or return
  incomplete answers to requests from datacenter/cloud IP ranges; if you run this on a VPS and
  see frequent 429s or gaps, an exit with a residential IP can help. At home you do not need one.
- **Port / host** — `127.0.0.1:8930` by default.

### Where the sales come from

**Groups** are read from Roblox's transaction-records API — the sales ledger the Creator Hub
itself shows (`GET apis.roblox.com/transaction-records-api/v2/groups/<id>/transactions`). It
is asked for time windows, so nothing is lost to pagination limits, and every sale carries:

- **where it happened** — in-game or website, so the In-game / Website card fills in for live
  data without a CSV import;
- **whether it is still held** — the sale's pending flag comes from the API's hold status;
- the creator's net Robux (what the dashboard counts) and the gross unit price (stored, not
  shown).

Things to know about it:

- **Rate budget: 50 requests per minute per account, shared by all your groups** (Roblox's
  published limit, in fixed 60-second windows). This app uses **at most 40 a minute**, leaving
  room for your own Creator Hub tab, and it obeys Roblox's `x-ratelimit-remaining` /
  `x-ratelimit-reset` and `retry-after` headers, so the dashboard and a `backfill` running at
  the same time still share the budget correctly. One request returns up to 100 sales.
- **History starts in mid-June 2026.** Windows before about 2026-06-16 come back empty. Whether
  that start is fixed or a rolling ~4-month window is not known yet; older months come from CSV
  imports.
- It is not an official, documented API. If it starts refusing a group (HTTP 403/404/400 three
  rounds in a row) or answers in a shape this app does not understand, that group **falls back
  to the older economy transactions API** — the status line says so, the In-game / Website split
  is missing for those sales, and the new API is retried every 6 hours. Throttling and network
  errors never trigger the fallback; they are retried.

**Your own account's store** (`track_user_sales`) has no transaction-records equivalent, so it
is read from the economy transactions API as before.

**Backfill.** The background sync starts at `initial_sync_days` before your first run. To pull
everything the API still holds, run the backfill once — it is never started automatically:

```bash
python -m roblox_sales backfill                        # every group, from 2026-06-16 to now
python -m roblox_sales backfill --store group:1111111 --since 2026-09-01
```

It reads one day at a time, oldest first, through the same 40-a-minute limit (roughly 4,000
sales a minute), and saves its progress after every day: stop it with Ctrl+C or let an error stop
it, run the same command again, and it continues where it stopped (`--restart` starts over).
Sales you already have — from the older API or a CSV import — are matched rather than
duplicated, and gain the location and hold status they were missing.

### Items On Sale and the Golden Ratio

Every 10 minutes (and when you click ↻ on the card, at most once a minute) the app counts each
group's items that are for sale, with the same read-only listing the Creator Hub's creations
page uses (`GET itemconfiguration.roblox.com/v1/items/by-creator`), one walk per asset type. An
item counts as on sale when it has a price. The asset types are configurable (`asset_types`);
the default is accessories and layered clothing.

A count is only replaced when **every** asset type of that group was read to the end. If one
type is rate-limited or errors, the card keeps the last complete count and shows its age — a
total of "the types that happened to answer" would silently undercount. A group that has never
had a complete count shows "–", never 0. "All stores" shows a total only when every combined
group has a complete count.

The **Golden Ratio** is `(pending Robux ÷ items on sale) ÷ average Robux per sale`, the average
taken over the sales in the selected store and date range: roughly how many typical sales'
worth of held Robux each listed item carries. It shows "–" when pending or the item count is
unknown, the count is 0, or there are no sales in range.

### CSV imports

The sales API's history starts in mid-June 2026, and the older economy API (your own account's
store, or a group on the fallback) only pages back about 10,000 sales per store. The Creator Hub's
monthly export (`Sales_Group<id>_<YYYY-MM>.csv` or `.csv.zip`) is the complete, final record of a
month and also says where each sale happened. Import it from the dashboard (drag and drop) or the
command line:

```bash
python -m roblox_sales import-csv ~/Downloads/Sales_Group1111111_2026-05.csv
python -m roblox_sales verify   ~/Downloads/Sales_Group1111111_2026-05.csv   # changes nothing
```

Importing a month **replaces** that month for that store. Safeguards:

- The filename must match exactly (a browser's ` (1)` suffix is refused) and the group must be in
  your config — an export for some other store is refused instead of being merged into your totals.
- A file with fewer than 90 % of the rows already stored for that month is refused as a likely
  truncated download. `import-csv --force` is the deliberate override.
- The same sale reported by both the API and the export (different ids, clocks up to ~80 s apart)
  is stored once. Rental payments — the same buyer paying for the same item several times with
  different amounts — are kept as separate sales.
- The sales API gives no transaction id, so its sales are keyed on buyer, asset, time (to the
  millisecond) and amount. When it reports a sale you already have from the older API or a CSV
  import, the two are matched — same buyer, asset and amount within 15 seconds (the clocks were
  measured 0.1–9 s apart), nearest first, and each stored sale matched at most once — so two
  real purchases a few seconds apart are still counted twice, as they should be.

## How syncing stays complete

- Each store has a **watermark** ("synced through"), and it only moves once everything before it
  was read to the end. An interrupted read is simply read again next time; a gap can never be
  stranded.
- **Groups (sales API):** each sync reads the window from the watermark (minus a 10-minute
  overlap) to one minute ago, following the API's cursor until it says there is no more. A long
  window — a first sync, or catching up after downtime — is read a day at a time, oldest first,
  and the watermark moves after each finished day. The background sync spends at most 60 requests
  per group per round and continues next round; `python -m roblox_sales sync` reads to the end.
- **Economy API** (your own store, or a group on the fallback): a sync walks newest → oldest until
  it passes the watermark. When Roblox stops paginating (HTTP 400 on a deep cursor), the walk is
  marked **truncated**, the rows it did read are kept, and the watermark stays put. The status
  line tells you to import the CSV export for that period. After you have,
  `python -m roblox_sales reset-watermark <store>` restarts that store's window.
- A store that fails does not stop the others, and only advances its own watermark on success.
- An empty answer where sales are already stored is treated as unknown, not as "no sales": the
  watermark stays and the read is retried.
- Sales API: one shared limiter, at most 40 requests a minute, obeying Roblox's rate-limit
  headers. Economy API: 429 → back off 30 s × attempt; 5xx / network → 10 s × attempt; pages are
  2 s apart and stores 3 s apart, because those endpoints share one rate budget per account.
- A failed financials read keeps the last-known numbers; nothing is ever blanked because a request
  failed.

## Cookie safety — read this

The tool reads your data with your `.ROBLOSECURITY` cookie. **That cookie is a full login to your
account**: anyone who has it can act as you, including spending Robux. Treat it like a password.

- **Never share it, paste it into chats, commit it, or put it in screenshots.** `config.ini`,
  `.env` and `roblox_cookie.txt` are git-ignored; keep it that way.
- Store it in `roblox_cookie.txt` with `chmod 600` (the app refuses a file other users can read on
  Linux/macOS), or in the `RSA_COOKIE` environment variable. It is never written to the database,
  never logged, and never shown in the dashboard.
- **The tool only reads.** Every request goes through a guard (`roblox_sales/roblox.py`) that
  refuses anything other than an HTTPS **GET** to `*.roblox.com` — including redirects — before
  it is sent. It never calls a logout, token or write endpoint, and the test suite checks that
  (`tests/test_readonly.py`). The cookie is only ever sent to `roblox.com`.
- Getting the value: sign in to roblox.com in your browser → developer tools → Application/Storage →
  Cookies → `.ROBLOSECURITY`. **Do not log out of that browser session afterwards** — logging out
  invalidates the cookie everywhere and the tool gets 401s until you paste a fresh one.
- Consider a dedicated browser profile for the session whose cookie you use, and revoke it from
  Roblox's security settings ("log out of all other sessions") if it ever leaks.

## Exposing the dashboard

By default the dashboard listens on `127.0.0.1` only, so nothing else on your network can reach it,
and no password is needed. To reach it from another machine:

1. `python -m roblox_sales hash-password` and put the result in `password_hash`.
2. Set `host = 0.0.0.0` (the app refuses to start on a non-loopback address without a password).
3. Set `allowed_hosts` to the host:port you browse to, and put it behind HTTPS (a reverse proxy)
   if it leaves your LAN.

With a password set, a **Log out** button appears in the top bar. Sessions are in memory
(restart = sign in again), the login is throttled per address, cookies are
`HttpOnly; SameSite=Strict`, the CSV upload needs a custom header a cross-site form cannot send,
and an unknown `Host` header is refused (DNS-rebinding protection).

## Docker (optional)

```bash
cp config.example.ini config.ini && cp .env.example .env
python -m roblox_sales hash-password     # → RSA_PASSWORD_HASH=... in .env
UID=$(id -u) GID=$(id -g) docker compose up -d --build
```

The container listens on all of *its* interfaces (so it requires a password); `docker-compose.yml`
publishes the port on `127.0.0.1` only and passes the cookie as a compose secret.

## Command line

```
python -m roblox_sales [serve] [--no-poll]   dashboard (+ background sync unless --no-poll)
python -m roblox_sales sync                  one sync of every store
python -m roblox_sales backfill [--store group:ID] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--restart]
                                             older group sales from the sales API (resumable)
python -m roblox_sales catalog               one count of items on sale per group
python -m roblox_sales import-csv FILES|DIRS [--force]
python -m roblox_sales verify FILES          compare exports with the database
python -m roblox_sales stats                 totals per store and watermarks
python -m roblox_sales reset-watermark SOURCE [--days N]
python -m roblox_sales hash-password
python -m roblox_sales demo [--serve]        synthetic data, no cookie, no network
```

## Development

```bash
python -m unittest discover -s tests -v
```

The tests run fully offline: every Roblox response is faked at the HTTP-adapter layer, and any
attempt to open a non-loopback socket fails the test.

## Known limits

- A sale's held/pending flag is recorded when it is synced and not watched afterwards (re-reading
  months of history every round to see holds release costs too many requests). Running
  `backfill --restart` re-reads group history and refreshes it; a CSV import makes a finished
  month final; the Pending Robux card always uses Roblox's own number.
- The sales API is undocumented; its behaviour (rate limit, history start, field meanings) was
  measured, not published. The fallback keeps sales flowing if it changes.
- Pending Robux, group funds and items on sale exist for groups only.
- Items on sale cover the configured asset types only; classic clothing is opt-in and
  unverified on the listing endpoint.

## License

MIT — see [LICENSE](LICENSE). Chart.js is vendored under its MIT license
([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
