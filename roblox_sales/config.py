"""Configuration: an INI file plus environment-variable overrides.

Precedence (highest first): process environment, a `.env` file next to the config file,
the INI file, built-in defaults. See config.example.ini and .env.example.

The Roblox cookie is deliberately NOT a field of the Config object's repr, is never logged,
and is only ever attached to requests for *.roblox.com (see roblox.py).
"""
from __future__ import annotations

import configparser
import ipaddress
import os
import stat
import sys
from dataclasses import dataclass, field

DEFAULT_PORT = 8930

# Asset types counted for "Items On Sale" (numeric Roblox asset type ids): accessories
# 8 Hat, 41 Hair, 42 Face, 43 Neck, 44 Shoulder, 45 Front, 46 Back, 47 Waist, and layered
# clothing 64-72 (T-shirt, Shirt, Pants, Jacket, Sweater, Shorts, Left/Right Shoe, Dress/Skirt).
# Classic clothing (2 T-Shirt, 11 Shirt, 12 Pants) is NOT in the default: whether the
# by-creator endpoint answers for it is unverified, and a type that never answers would keep
# every count from completing. Add it with `asset_types` if it works for you.
DEFAULT_ASSET_TYPES = (8, 41, 42, 43, 44, 45, 46, 47, 64, 65, 66, 67, 68, 69, 70, 71, 72)


class ConfigError(Exception):
    """A configuration problem the user has to fix. The message never contains a secret."""


@dataclass
class Store:
    """One sales store: a group, or the authenticated user's own account."""
    source: str            # stable key stored in the database: "group:<id>" or "user"
    name: str              # display name — free text, changing it never re-keys any data
    group_id: int | None = None
    combine: bool = True   # included in "All stores" / combined totals


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    db_path: str = "data/sales.db"
    proxy: str = ""
    password_hash: str = ""
    allowed_hosts: list[str] = field(default_factory=list)
    track_user_sales: bool = True
    user_store_name: str = "My account"
    sync_interval: int = 180
    financials_interval: int = 120
    balance_interval: int = 120
    initial_sync_days: int = 90
    catalog_interval: int = 600
    asset_types: list[int] = field(default_factory=lambda: list(DEFAULT_ASSET_TYPES))
    cookie_file: str = ""
    stores: list[Store] = field(default_factory=list)
    base_dir: str = "."
    # never shown in repr(), so a stray print(config) cannot leak it
    _cookie: str = field(default="", repr=False)

    @property
    def groups(self) -> list[Store]:
        return [s for s in self.stores if s.group_id is not None]

    def store(self, source: str) -> Store | None:
        for s in self.stores:
            if s.source == source:
                return s
        return None

    def group_store(self, gid: int) -> Store | None:
        return self.store(f"group:{int(gid)}")

    @property
    def is_loopback(self) -> bool:
        if self.host in ("localhost",):
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def cookie(self) -> str:
        """The .ROBLOSECURITY value, or "" when none is configured (the dashboard still runs)."""
        return self._cookie


# ── helpers ───────────────────────────────────────────────────────────────────

def _bool(v, default=False) -> bool:
    if v is None or str(v).strip() == "":
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"expected true/false, got {v!r}")


def _int(v, default: int, name: str, lo: int | None = None) -> int:
    if v is None or str(v).strip() == "":
        return default
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {v!r}") from None
    if lo is not None and n < lo:
        raise ConfigError(f"{name} must be at least {lo}")
    return n


def load_dotenv(path: str, environ=None) -> None:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Existing
    environment variables always win, so a real env var can override the file."""
    environ = os.environ if environ is None else environ
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            environ.setdefault(k, v)


def _check_secret_file_mode(path: str) -> None:
    """Refuse a cookie file other local users can read. Skipped on Windows, where POSIX
    mode bits do not describe the file's real access control."""
    if os.name == "nt":
        return
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"the cookie file {path} is readable by other users "
            f"(mode {stat.S_IMODE(mode):o}); run: chmod 600 {path}")


def read_cookie(cookie_env: str, cookie_file: str) -> str:
    """The cookie from RSA_COOKIE, else from the cookie file. "" when neither is set."""
    if cookie_env.strip():
        return cookie_env.strip()
    if cookie_file and os.path.isfile(cookie_file):
        _check_secret_file_mode(cookie_file)
        with open(cookie_file, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def parse_groups_env(value: str) -> list[tuple[int, str]]:
    """RSA_GROUPS="123:My Store,456:Other Store" → [(123, "My Store"), (456, "Other Store")]."""
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        gid, _, name = part.partition(":")
        try:
            gid_i = int(gid.strip())
        except ValueError:
            raise ConfigError(f"RSA_GROUPS entry {part!r} must look like <group id>:<name>") from None
        out.append((gid_i, name.strip() or f"Group {gid_i}"))
    return out


# ── loader ────────────────────────────────────────────────────────────────────

def load(path: str | None = None, environ=None, read_secret: bool = True) -> Config:
    environ = dict(os.environ) if environ is None else dict(environ)
    path = path or environ.get("RSA_CONFIG") or "config.ini"
    base_dir = os.path.dirname(os.path.abspath(path))
    load_dotenv(os.path.join(base_dir, ".env"), environ)

    cp = configparser.ConfigParser(interpolation=None)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    app = cp["app"] if cp.has_section("app") else {}

    def get(key, env):
        v = environ.get(env)
        return v if v is not None and v != "" else app.get(key)

    cfg = Config(base_dir=base_dir)
    cfg.host = (get("host", "RSA_HOST") or cfg.host).strip()
    cfg.port = _int(get("port", "RSA_PORT"), cfg.port, "port", lo=1)
    cfg.db_path = (get("db_path", "RSA_DB_PATH") or cfg.db_path).strip()
    if not os.path.isabs(cfg.db_path):
        cfg.db_path = os.path.join(base_dir, cfg.db_path)
    cfg.proxy = (get("proxy", "RSA_PROXY") or "").strip()
    cfg.password_hash = (get("password_hash", "RSA_PASSWORD_HASH") or "").strip()
    cfg.allowed_hosts = [h.strip().lower() for h in
                         (get("allowed_hosts", "RSA_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    cfg.track_user_sales = _bool(get("track_user_sales", "RSA_TRACK_USER_SALES"), True)
    cfg.user_store_name = (get("user_store_name", "RSA_USER_STORE_NAME") or cfg.user_store_name).strip()
    cfg.sync_interval = _int(get("sync_interval", "RSA_SYNC_INTERVAL"), 180, "sync_interval", lo=60)
    cfg.financials_interval = _int(get("financials_interval", "RSA_FINANCIALS_INTERVAL"), 120,
                                   "financials_interval", lo=60)
    cfg.balance_interval = _int(get("balance_interval", "RSA_BALANCE_INTERVAL"), 120,
                                "balance_interval", lo=60)
    cfg.initial_sync_days = _int(get("initial_sync_days", "RSA_INITIAL_SYNC_DAYS"), 90,
                                 "initial_sync_days", lo=1)
    cfg.catalog_interval = _int(get("catalog_interval", "RSA_CATALOG_INTERVAL"), 600,
                                "catalog_interval", lo=600)
    types = get("asset_types", "RSA_ASSET_TYPES")
    if types is not None and types.strip():
        try:
            cfg.asset_types = [int(t) for t in types.replace(" ", "").split(",") if t]
        except ValueError:
            raise ConfigError("asset_types must be comma-separated numeric asset type ids") from None

    # groups: [group:<id>] sections, plus RSA_GROUPS from the environment
    seen = set()
    for sec in cp.sections():
        if not sec.lower().startswith("group:"):
            continue
        try:
            gid = int(sec.split(":", 1)[1].strip())
        except ValueError:
            raise ConfigError(f"section [{sec}] must be [group:<numeric group id>]") from None
        s = cp[sec]
        cfg.stores.append(Store(source=f"group:{gid}", name=(s.get("name") or f"Group {gid}").strip(),
                                group_id=gid, combine=_bool(s.get("combine"), True)))
        seen.add(gid)
    for gid, name in parse_groups_env(environ.get("RSA_GROUPS", "")):
        if gid not in seen:
            cfg.stores.append(Store(source=f"group:{gid}", name=name, group_id=gid))
            seen.add(gid)
    if cfg.track_user_sales:
        cfg.stores.append(Store(source="user", name=cfg.user_store_name))

    cfg.cookie_file = (get("cookie_file", "RSA_COOKIE_FILE") or "roblox_cookie.txt").strip()
    if not os.path.isabs(cfg.cookie_file):
        cfg.cookie_file = os.path.join(base_dir, cfg.cookie_file)
    if read_secret:
        cfg._cookie = read_cookie(environ.get("RSA_COOKIE", ""), cfg.cookie_file)

    if not cfg.is_loopback and not cfg.password_hash:
        raise ConfigError(
            f"host = {cfg.host} exposes the dashboard beyond this machine; set password_hash "
            f"first (python -m roblox_sales hash-password), or bind to 127.0.0.1")
    return cfg


def warn(msg: str) -> None:
    print(f"[config] {msg}", file=sys.stderr, flush=True)
