# Roblox Sales Analytics

A self-hosted, **read-only** sales dashboard for Roblox UGC creators. It keeps your own copy of
your group (and account) sales in a local SQLite database and shows them the way you actually
want to look at them: per-day charts with the top items broken out, an items table, group funds
and pending Robux, items on sale, a "golden ratio", your current Robux balance, and Creator Hub
CSV imports for months the API can no longer page back to.

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
| In-game vs website split | CSV imports (the API does not report it) |
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

Roblox's transaction API only pages back about 10,000 sales per store, and its hold status for old
sales goes stale. The Creator Hub's monthly export (`Sales_Group<id>_<YYYY-MM>.csv` or `.csv.zip`)
is the complete, final record of a month and also says where each sale happened. Import it from the
dashboard (drag and drop) or the command line:

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

## How syncing stays complete

- Each store has a **watermark** ("synced through"). A sync walks that store's sales newest →
  oldest until it passes the watermark, and the watermark only moves after the walk **completed**.
  An interrupted walk is simply walked again next time; a gap can never be stranded.
- When Roblox stops paginating (HTTP 400 on a deep cursor), the walk is marked **truncated**, the
  rows it did read are kept, and the watermark stays put. The status line tells you to import the
  CSV export for that period. After you have, `python -m roblox_sales reset-watermark group:<id>`
  restarts that store's window.
- A store that fails does not stop the others, and only advances its own watermark on success.
- An empty page where sales already exist is treated as an unknown answer, not as "no sales".
- 429 → back off 30 s × attempt; 5xx / network → 10 s × attempt. Pages are 2 s apart and stores 3 s
  apart, because the economy endpoints share one rate budget per account.
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

- `is_pending` on API-synced rows is not refreshed after the sale (re-walking months of history to
  watch the ~30-day hold flip costs too many requests); the Pending Robux card uses Roblox's own
  number instead, and a CSV import makes a finished month final.
- Pending Robux, group funds and items on sale exist for groups only.
- Items on sale cover the configured asset types only; classic clothing is opt-in and
  unverified on the listing endpoint.

## License

MIT — see [LICENSE](LICENSE). Chart.js is vendored under its MIT license
([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
