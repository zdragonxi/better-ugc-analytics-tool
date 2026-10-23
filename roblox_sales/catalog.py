"""Items On Sale: how many of a group's items are currently for sale.

Read with the same endpoint the Creator Hub's creations page uses,
`GET itemconfiguration.roblox.com/v1/items/by-creator`, one walk per asset type. An item is on
sale exactly when its `price` is not null.

The rule that keeps the number honest: a refresh REPLACES the stored count only when every
asset type of that group was read to the end. A count assembled from the types that happened
to answer is not "the count" — it silently undercounts whenever one type is rate-limited, and
looks perfectly plausible while doing so. So a partial refresh keeps the last complete count
(shown with its age), and a store that has never had a complete refresh shows "–", not 0.
"""
from __future__ import annotations

import json
import time

from . import roblox, store

BY_CREATOR_URL = roblox.ITEMS_BY_CREATOR_URL
PAGE_SIZE = 30
PAGE_SLEEP = 0.5        # between pages; the endpoint is lenient, this keeps us polite
MAX_PAGES = 400         # 12,000 items per type — a runaway cursor chain stops here


class Incomplete(RuntimeError):
    """This asset type could not be read to the end: its count is unknown."""


def count_type(s, gid: int, asset_type: int, sleep=time.sleep) -> int:
    """On-sale items of one asset type in one group, or Incomplete."""
    on_sale = 0
    cursor = ""
    for page in range(MAX_PAGES):
        params = {"assetType": asset_type, "groupId": gid, "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        try:
            _, j = roblox.get_json(s, BY_CREATOR_URL, params=params, sleep=sleep)
        except (roblox.AuthError, roblox.ReadOnlyViolation):
            raise
        except Exception as e:          # noqa: BLE001 — any failure: this type is unknown
            raise Incomplete(f"type {asset_type}: {roblox.safe_error(e)}") from None
        items = j.get("items") if isinstance(j, dict) else None
        if not isinstance(items, list):
            raise Incomplete(f"type {asset_type}: unexpected answer shape")
        if not items and cursor:
            # an empty page reached through a cursor is not a believable end of the list
            raise Incomplete(f"type {asset_type}: empty page mid-list")
        on_sale += sum(1 for it in items if isinstance(it, dict) and it.get("price") is not None)
        cursor = j.get("nextCursor") or ""
        if not cursor:
            return on_sale
        sleep(PAGE_SLEEP)
    raise Incomplete(f"type {asset_type}: more than {MAX_PAGES} pages")


def refresh_group(prev: dict | None, s, gid: int, asset_types, sleep=time.sleep,
                  now: float | None = None) -> dict:
    """Walk every asset type of one group and return its new catalog record.

    Record: {"items": int|None, "as_of": epoch|None, "types": {type: n}, "attempt": epoch,
    "error": str|None}. `items`/`as_of`/`types` change only when EVERY type completed."""
    rec = dict(prev or {"items": None, "as_of": None, "types": {}})
    rec["attempt"] = int(now if now is not None else time.time())
    counts, errors = {}, []
    for i, t in enumerate(asset_types):
        if i:
            sleep(PAGE_SLEEP)
        try:
            counts[str(t)] = count_type(s, gid, t, sleep=sleep)
        except Incomplete as e:
            errors.append(str(e))
    if errors:
        rec["error"] = "; ".join(errors)[:300]
        return rec
    total = sum(counts.values())
    if total == 0 and (rec.get("items") or 0) > 0:
        # every type answered "nothing for sale" where items were for sale before: an
        # all-empty answer is far more likely a bad reply than a whole catalogue going
        # off sale at once, so it is not taken as the new truth
        rec["error"] = "every type came back empty — kept the last complete count"
        return rec
    rec.update(items=total, as_of=rec["attempt"], types=counts, error=None)
    return rec


def refresh_all(db, s, cfg, sleep=time.sleep) -> dict:
    """Refresh every configured group and persist the records (meta key 'catalog')."""
    snap = store.get_json_meta(db, "catalog", {}) or {}
    for i, st in enumerate(cfg.groups):
        if i:
            sleep(1.0)
        snap[st.source] = refresh_group(snap.get(st.source), s, st.group_id, cfg.asset_types,
                                        sleep=sleep)
        store.set_meta(db, "catalog", json.dumps(snap))     # persist after each group
    return snap
