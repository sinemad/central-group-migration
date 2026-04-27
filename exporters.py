"""
exporters.py
============
Export logic for Classic Central AOS10 groups.

Simplified to capture only the data needed for New Central migration:
  1. AP inventory  — serial, name, model, IP for every AP in the group
  2. AP settings   — per-AP hostname/zone/radio settings

Both exporters run for every group regardless of type. Groups with no APs
(e.g. switch-only groups) produce an empty inventory and no settings files.
"""

import json
import os

from pycentral.classic.configuration import ApConfiguration, ApSettings

# ---------------------------------------------------------------------------
# Disk helpers
# ---------------------------------------------------------------------------

def _save(group_dir: str, filename: str, data):
    os.makedirs(group_dir, exist_ok=True)
    with open(os.path.join(group_dir, filename), "w") as f:
        json.dump(data, f, indent=2)


def _load(group_dir: str, filename: str):
    p = os.path.join(group_dir, filename)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Shared module instances
# ---------------------------------------------------------------------------
_ap_sett = ApSettings()
_ap_cfg  = ApConfiguration()


# ---------------------------------------------------------------------------
# AP list helper — shared by both exporters
# ---------------------------------------------------------------------------

def _get_aps_in_group(central, group_name: str) -> list:
    """Return all AP dicts for APs assigned to group_name.

    GET /monitoring/v2/aps — paginated, handles all known response formats:
      {"aps": [...], "total": N}   current
      {"data": [...], "total": N}  older clusters
      [...]                        bare list (rare)
    """
    aps, offset, limit = [], 0, 100
    while True:
        resp = central.command(
            apiMethod="GET",
            apiPath="/monitoring/v2/aps",
            apiParams={"group": group_name, "limit": limit, "offset": offset},
        )
        if resp["code"] != 200:
            print(f"    [WARN] GET /monitoring/v2/aps HTTP {resp['code']} for {group_name}")
            break
        msg = resp["msg"]
        if isinstance(msg, list):
            page, total = msg, len(msg)
        elif isinstance(msg, dict):
            page  = msg.get("aps") or msg.get("data") or []
            total = msg.get("total", 0)
        else:
            break
        aps.extend(page)
        if len(page) < limit or (total > 0 and offset + limit >= total):
            break
        offset += limit
    return aps


# ---------------------------------------------------------------------------
# 1. AP Inventory  (all groups)
#
#    Saves: ap_inventory.json
#    [{serial, name, model, ip}, ...]
#
#    This is the authoritative AP list for a group. Serial + name are the
#    primary fields; model and ip are included for reference.
# ---------------------------------------------------------------------------

def export_ap_inventory(central, group_name: str, group_dir: str, **_):
    """Save AP inventory for all APs in the group.

    Calls GET /monitoring/v2/aps and records serial, name, model, and IP
    for every AP. Groups with no APs write an empty array.
    """
    aps = _get_aps_in_group(central, group_name)

    inventory = []
    for ap in aps:
        serial = ap.get("serial") or ap.get("serial_number", "")
        if not serial:
            continue
        inventory.append({
            "serial": serial,
            "name":   ap.get("name") or ap.get("hostname", ""),
            "model":  ap.get("model", ""),
            "ip":     ap.get("ip_address") or ap.get("ip", ""),
        })

    _save(group_dir, "ap_inventory.json", inventory)
    print(f"    ap_inventory.json   ({len(inventory)} APs)")
    return {"file": "ap_inventory.json", "status": "ok",
            "detail": f"{len(inventory)} APs"}


def import_ap_inventory(*_, **__) -> bool:
    """No-op — inventory is used by the New Central importer, not re-pushed."""
    return True


# ---------------------------------------------------------------------------
# 2. AP Settings  (all groups)
#
#    Saves: ap_settings/<serial>.json
#    One file per AP that has settings — hostname, zone, radio config.
#
#    GET /configuration/v2/ap_settings/{serial}
# ---------------------------------------------------------------------------

def export_ap_cli_config(central, group_name: str, group_dir: str, **_):
    """Export the group-level AP CLI configuration.

    Calls GET /configuration/v2/ap_cli/{group} and saves ap_cli_config.json.
    """
    resp = _ap_cfg.get_ap_config(central, group_name)
    if resp["code"] != 200:
        print(f"    ap_cli_config.json  [HTTP {resp['code']} - skipped]")
        return {"file": "ap_cli_config.json", "status": "warn",
                "detail": f"HTTP {resp['code']}"}
    _save(group_dir, "ap_cli_config.json", {"cli_config": resp["msg"]})
    lines = len(resp["msg"]) if isinstance(resp["msg"], (list, str)) else 0
    print(f"    ap_cli_config.json  ({lines} CLI lines)")
    return {"file": "ap_cli_config.json", "status": "ok",
            "detail": f"{lines} CLI lines"}


def import_ap_cli_config(*_, **__) -> bool:
    """No-op — CLI config is reference data, not re-pushed via import."""
    return True


def export_ap_settings(central, group_name: str, group_dir: str, **_):
    """Export per-AP settings for all APs in the group.

    Saves one file per AP under ap_settings/<serial>.json.
    APs that return non-200 from the settings endpoint are skipped.
    """
    aps = _get_aps_in_group(central, group_name)
    if not aps:
        print(f"    ap_settings/        [no APs found - skipped]")
        return {"file": "ap_settings", "status": "ok", "detail": "0 APs"}

    sett_dir = os.path.join(group_dir, "ap_settings")
    os.makedirs(sett_dir, exist_ok=True)
    saved = 0

    for ap in aps:
        serial = ap.get("serial") or ap.get("serial_number", "")
        if not serial:
            continue
        resp = _ap_sett.get_ap_settings(central, serial)
        if resp["code"] == 200:
            _save(sett_dir, f"{serial}.json", resp["msg"])
            saved += 1

    print(f"    ap_settings/        ({saved} of {len(aps)} APs)")
    return {"file": "ap_settings", "status": "ok",
            "detail": f"{saved}/{len(aps)} APs"}


def import_ap_settings(central, group_name: str, group_dir: str,
                        import_name: str = None) -> bool:
    """Re-apply per-AP settings using ApSettings.update_ap_settings()."""
    sett_dir = os.path.join(group_dir, "ap_settings")
    if not os.path.isdir(sett_dir):
        return True

    files  = [f for f in os.listdir(sett_dir) if f.endswith(".json")]
    ok_all = True

    for fname in files:
        data   = _load(sett_dir, fname)
        serial = fname.replace(".json", "")
        resp   = _ap_sett.update_ap_settings(central, serial, data)
        if resp["code"] in (200, 201):
            print(f"  [{group_name}] AP settings applied: {serial}")
        else:
            print(f"  [{group_name}] AP settings failed:  {serial} - {resp['code']}: {resp['msg']}")
            ok_all = False

    return ok_all


# ---------------------------------------------------------------------------
# Exporter registry
# ---------------------------------------------------------------------------

EXPORTERS = [
    {
        "name":       "ap_inventory",
        "applies_to": None,
        "export_fn":  export_ap_inventory,
        "import_fn":  import_ap_inventory,
    },
    {
        "name":       "ap_cli_config",
        "applies_to": None,
        "export_fn":  export_ap_cli_config,
        "import_fn":  import_ap_cli_config,
    },
    {
        "name":       "ap_settings",
        "applies_to": None,
        "export_fn":  export_ap_settings,
        "import_fn":  import_ap_settings,
    },
]


def get_active_exporters(allowed_types: list) -> list:
    """Return all exporters — both now apply to every group type."""
    return list(EXPORTERS)
