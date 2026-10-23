"""Command line: python -m roblox_sales <command>  (run with --help for the list)."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone

from . import __version__, auth, config, roblox, store, sync


def _load(args, read_secret=True):
    demo_mode = getattr(args, "demo", False)
    try:
        # the demo never reads a cookie, and ignores RSA_* variables so it cannot be pointed
        # at a real database by accident
        return config.load(args.config, environ={} if demo_mode else None,
                           read_secret=read_secret and not demo_mode)
    except config.ConfigError as e:
        sys.exit(f"config error: {e}")


def cmd_serve(args):
    cfg = _load(args)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if not cfg.is_loopback and not cfg.password_hash:
        sys.exit("refusing to listen beyond this machine without password_hash "
                 "(python -m roblox_sales hash-password)")
    from .poller import Poller
    from .web import serve
    store.connect(cfg.db_path).close()
    poller = Poller(cfg)
    if args.no_poll:
        poller.state["sync_error"] = None
    else:
        poller.start()
    httpd = serve(cfg, poller)
    shown = "localhost" if cfg.is_loopback else cfg.host
    print(f"Sales Analytics {__version__} on http://{shown}:{cfg.port}  "
          f"({'polling Roblox' if poller.state['polling'] else 'not polling'}; Ctrl+C to stop)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        httpd.server_close()


def cmd_sync(args):
    cfg = _load(args)
    if not cfg.cookie():
        sys.exit("no cookie configured (RSA_COOKIE or the cookie file) — see README")
    db = store.connect(cfg.db_path)
    s = roblox.make_session(cfg.cookie(), cfg.proxy)
    try:
        me = roblox.authenticated_user(s)
        print(f"Cookie accepted. Syncing {len(cfg.stores)} store(s)...", flush=True)
        if cfg.groups:
            sync.refresh_financials(db, s, cfg)
        bal = sync.fetch_balance(s)
        if bal is not None:
            store.set_balance(db, bal)
        results = sync.sync_stores(db, s, cfg, me["id"])
    except roblox.AuthError as e:
        sys.exit(str(e))
    for r in results:
        name = cfg.store(r.source).name if cfg.store(r.source) else r.source
        state = ("TRUNCATED (import the CSV export)" if r.truncated else
                 f"ERROR {r.error}" if r.error else "ok")
        print(f"  {name:<24} {r.pages:>4} page(s)  {r.new:>6} new  {state}")
    if any(r.error or r.truncated for r in results):
        sys.exit(1)


def cmd_catalog(args):
    from . import catalog
    cfg = _load(args)
    if not cfg.cookie():
        sys.exit("no cookie configured (RSA_COOKIE or the cookie file) — see README")
    if not cfg.groups:
        sys.exit("no groups configured")
    db = store.connect(cfg.db_path)
    s = roblox.make_session(cfg.cookie(), cfg.proxy)
    try:
        snap = catalog.refresh_all(db, s, cfg)
    except roblox.AuthError as e:
        sys.exit(str(e))
    bad = False
    for st in cfg.groups:
        r = snap.get(st.source) or {}
        n = "–" if r.get("items") is None else f"{r['items']:,}"
        print(f"  {st.name:<24} {n:>8} on sale" + (f"   INCOMPLETE: {r['error']}" if r.get("error") else ""))
        bad = bad or bool(r.get("error"))
    if bad:
        sys.exit(1)


def cmd_import(args):
    cfg = _load(args, read_secret=False)
    db = store.connect(cfg.db_path)
    known = [s.group_id for s in cfg.groups]
    files = []
    for p in args.paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.lower().endswith((".csv", ".csv.zip"))]
        else:
            files.append(p)
    failed = 0
    for f in files:
        try:
            r = store.import_csv(db, f, known, force=args.force)
            print(f"  {r['filename']}: {r['rows']:,} rows for {r['month']} "
                  f"(replaced {r['replaced']:,})")
        except store.ImportRefused as e:
            failed += 1
            print(f"  {os.path.basename(f)}: NOT imported — {e}")
    if failed:
        sys.exit(1)


def cmd_verify(args):
    cfg = _load(args, read_secret=False)
    db = store.connect(cfg.db_path)
    known = [s.group_id for s in cfg.groups]
    ok = True
    for f in args.paths:
        r = store.verify_csv(db, f, known)
        print(f"  {r['filename']}: {r['rows']:,} rows, {len(r['missing']):,} missing from the database")
        for m in r["missing"][:5]:
            print(f"    missing: {m['date']}  {m['item']}  {m['robux']}")
        ok = ok and not r["missing"]
    print("VERIFY PASSED" if ok else "VERIFY FAILED")
    if not ok:
        sys.exit(1)


def cmd_stats(args):
    cfg = _load(args, read_secret=False)
    db = store.connect(cfg.db_path)
    n, rbx = db.execute("SELECT COUNT(*), COALESCE(SUM(robux),0) FROM sales").fetchone()
    print(f"{n:,} sales, {rbx:,} Robux")
    for src, c, r in db.execute("SELECT source, COUNT(*), COALESCE(SUM(robux),0) FROM sales "
                                "GROUP BY source ORDER BY 3 DESC"):
        st = cfg.store(src)
        wm = store.get_watermark(db, src)
        when = f"synced through {wm:%Y-%m-%d %H:%M} UTC" if wm else "never synced by the API"
        print(f"  {(st.name if st else src):<24} {c:>8,} sales {r:>12,} Robux   {when}")


def cmd_reset_watermark(args):
    """Acknowledge a history gap you closed with CSV imports (or accept it): the next sync
    walks back only `--days` from now."""
    cfg = _load(args, read_secret=False)
    db = store.connect(cfg.db_path)
    st = cfg.store(args.source)
    if not st:
        sys.exit(f"unknown store {args.source!r}; known: "
                 + ", ".join(s.source for s in cfg.stores))
    store.set_watermark(db, st.source, datetime.now(timezone.utc) - timedelta(days=args.days))
    print(f"{st.name}: next sync walks back {args.days} day(s)")


def cmd_hash_password(args):
    pw = getpass.getpass("New dashboard password: ")
    if len(pw) < 10:
        sys.exit("use at least 10 characters")
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("the two entries differ")
    print("Put this in config.ini as password_hash (or RSA_PASSWORD_HASH):")
    print(auth.hash_password(pw))


def cmd_demo(args):
    from . import demo
    path = os.path.abspath(args.db or os.path.join("data", "demo.db"))
    ini = os.path.splitext(path)[0] + ".ini"
    demo.build(path, seed=args.seed)
    demo.write_config(ini, os.path.basename(path), port=args.port or config.DEFAULT_PORT)
    print(f"Synthetic demo database: {path}")
    print(f"Demo config: {ini}")
    if args.serve:
        args.config, args.demo, args.no_poll, args.host, args.port = ini, True, True, None, None
        cmd_serve(args)


def _console_utf8():
    """Messages contain a few non-ASCII characters (dashes, arrows). A Windows pipe or a
    terminal emulator gets the legacy code page by default and shows them as mojibake, so
    switch the text streams to UTF-8 when they are not already; never fail over it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "").replace("_", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    _console_utf8()
    ap = argparse.ArgumentParser(prog="python -m roblox_sales",
                                 description="Self-hosted, read-only Roblox sales dashboard")
    ap.add_argument("--config", default=None, help="config file (default: RSA_CONFIG or config.ini)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the dashboard (default)")
    p.add_argument("--host"); p.add_argument("--port", type=int)
    p.add_argument("--no-poll", action="store_true", help="serve stored data, never call Roblox")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("sync", help="one sync of every store, then exit")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("catalog", help="one count of items on sale per group, then exit")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("import-csv", help="import Creator Hub monthly exports (files or folders)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--force", action="store_true",
                   help="import even when the file has far fewer rows than are stored")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("verify", help="check exports against the database (changes nothing)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("stats", help="print a summary of the database")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reset-watermark", help="after a history gap: restart a store's sync window")
    p.add_argument("source", help="group:<id> or user")
    p.add_argument("--days", type=int, default=2)
    p.set_defaults(func=cmd_reset_watermark)

    p = sub.add_parser("hash-password", help="make a password_hash for the config")
    p.set_defaults(func=cmd_hash_password)

    p = sub.add_parser("demo", help="build a SYNTHETIC demo database (no cookie, no network)")
    p.add_argument("--db"); p.add_argument("--seed", type=int, default=7)
    p.add_argument("--serve", action="store_true", help="then serve it without polling")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_demo)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):          # no command → serve
        args.func, args.host, args.port, args.no_poll = cmd_serve, None, None, False
    args.func(args)


if __name__ == "__main__":
    main()
