"""
exporters.py
============
Per-data-type export/import logic for Classic Central (AOS10 UI groups).

All API calls use the official pycentral.classic module classes:
  - pycentral.classic.configuration.Groups
  - pycentral.classic.configuration.ApConfiguration
  - pycentral.classic.configuration.Wlan
  - pycentral.classic.configuration.ApSettings
  - pycentral.classic.configuration.Devices

The exporter registry (EXPORTERS) is the single place to add new data types.
Each entry declares:
  name        - file stem written to disk
  applies_to  - set of allowed_types that activate it (None = all groups)
  export_fn   - writes file(s) to group_dir
  import_fn   - reads file(s) and calls Central API

Neither export_groups.py, import_groups.py, nor app.py need to change
when a new exporter is added here.
"""

import json
import os

from pycentral.classic.configuration import (
    ApConfiguration,
    ApSettings,
    Devices,
    Groups,
    Wlan,
)

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
# Shared module instances (stateless, safe to reuse)
# ---------------------------------------------------------------------------
_ap_config = ApConfiguration()
_wlan      = Wlan()
_ap_sett   = ApSettings()
_devices   = Devices()


# ---------------------------------------------------------------------------
# 1. Group properties  (all groups)
# ---------------------------------------------------------------------------

def export_properties(central, group_name: str, group_dir: str,
                       properties: dict, **_):
    """Write pre-fetched group properties to disk.

    properties is passed in from _get_group_properties() in app.py /
    export_groups.py - it is already fetched at group-list time to avoid
    a redundant API call per group.
    """
    _save(group_dir, "properties.json", properties)
    print(f"    properties.json")
    return {"file": "properties.json", "status": "ok"}


def import_properties(central, group_name: str, group_dir: str,
                       import_name: str = None) -> bool:
    """Create the group on the target instance.

    Uses import_name as the created name when a rename has been set,
    otherwise falls back to the original group_name.
    """
    properties = _load(group_dir, "properties.json")
    if properties is None:
        print(f"  [{group_name}] Missing properties.json - cannot create group")
        return False

    target = import_name or group_name
    payload = {
        "group": target,
        "group_attributes": {
            "template_info": {"Wired": False, "Wireless": False},
            "group_properties": properties,
        },
    }
    resp = central.command(
        apiMethod="POST",
        apiPath="/configuration/v3/groups",
        apiData=payload,
    )
    label = f"{group_name} -> {target}" if target != group_name else group_name
    if resp["code"] in (200, 201):
        print(f"  [{label}] Group created")
        return True
    print(f"  [{label}] Failed to create group - {resp['code']}: {resp['msg']}")
    return False


# ---------------------------------------------------------------------------
# 2. AP group CLI config  (IAP groups - AOS10)
#
#    Uses ApConfiguration.get_ap_config(conn, group_name)
#    which calls GET /configuration/v1/ap_cli/{group_name}
# ---------------------------------------------------------------------------

def export_ap_cli_config(central, group_name: str, group_dir: str, **_):
    """Export the full AOS10 CLI configuration for the group.

    Calls ApConfiguration.get_ap_config(conn, group_name)
    -> GET /configuration/v1/ap_cli/{group_name}

    The API returns a JSON list of CLI strings, or raw text depending on
    the Central cluster. Both are normalised to a list before saving.
    """
    resp = _ap_config.get_ap_config(central, group_name)
    if resp["code"] != 200:
        print(f"    ap_cli_config.json  [WARN: HTTP {resp['code']}: {resp['msg']}]")
        return {"file": "ap_cli_config.json", "status": "warn",
                "detail": f"HTTP {resp['code']}"}

    msg = resp["msg"]
    # Normalise: API may return a list, a newline-delimited string, or a dict
    if isinstance(msg, list):
        cli_lines = msg
    elif isinstance(msg, str):
        cli_lines = [l for l in msg.splitlines() if l]
    elif isinstance(msg, dict):
        # Some versions wrap in {"config": [...]} or {"clis": [...]}
        cli_lines = msg.get("clis") or msg.get("config") or msg.get("cli", [])
        if isinstance(cli_lines, str):
            cli_lines = [l for l in cli_lines.splitlines() if l]
    else:
        cli_lines = []

    _save(group_dir, "ap_cli_config.json", {"cli_config": cli_lines})
    print(f"    ap_cli_config.json  ({len(cli_lines)} CLI lines)")
    return {"file": "ap_cli_config.json", "status": "ok",
            "detail": f"{len(cli_lines)} lines"}


def import_ap_cli_config(central, group_name: str, group_dir: str,
                          import_name: str = None) -> bool:
    """Re-push the group CLI config using ApConfiguration.replace_ap().

    This is a full replace - ensure the target group is empty before import.
    Calls POST /configuration/v1/ap_cli/{group_name} with {"clis": [...]}.
    """
    data = _load(group_dir, "ap_cli_config.json")
    if data is None:
        return True

    target  = import_name or group_name
    cli_cfg = data.get("cli_config", [])
    payload = {"clis": cli_cfg} if isinstance(cli_cfg, list) else cli_cfg
    resp    = _ap_config.replace_ap(central, target, payload)
    if resp["code"] in (200, 201):
        print(f"  [{group_name}] Group CLI config pushed")
        return True
    print(f"  [{group_name}] Group CLI config failed - {resp['code']}: {resp['msg']}")
    return False


# ---------------------------------------------------------------------------
# 3. Per-device AP CLI config  (IAP groups - AOS10)
#
#    AOS10 supports device-level configuration overrides.
#    We list APs in the group via the monitoring API, then call
#    ApConfiguration.get_ap_config(conn, serial) for each AP.
#    Only APs with non-empty config are written to disk.
# ---------------------------------------------------------------------------

def _get_aps_in_group(central, group_name: str) -> list:
    """Return list of AP dicts for all APs assigned to group_name.

    Uses GET /monitoring/v2/aps with group filter. Handles pagination and
    all known response format variants across Classic Central clusters:

      {"aps": [...], "total": N}         current format
      {"data": [...], "total": N}        older clusters
      [...]                              bare list (rare)
    """
    aps, offset, limit = [], 0, 100
    while True:
        resp = central.command(
            apiMethod="GET",
            apiPath="/monitoring/v2/aps",
            apiParams={"group": group_name, "limit": limit, "offset": offset},
        )
        if resp["code"] != 200:
            print(f"    [WARN] GET /monitoring/v2/aps HTTP {resp['code']} for group {group_name}")
            break
        msg = resp["msg"]

        # Normalise response format
        if isinstance(msg, list):
            page  = msg
            total = len(msg)
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


def export_device_ap_configs(central, group_name: str, group_dir: str, **_):
    """Export per-device CLI config for APs with unique (override) configs.

    Calls ApConfiguration.get_ap_config(conn, serial) for each AP in
    the group. Saves only devices with non-empty config under:
        <group_dir>/device_ap_configs/<serial>.json
    """
    aps = _get_aps_in_group(central, group_name)
    if not aps:
        print(f"    device_ap_configs/  [no APs found in group - skipped]")
        return

    dev_dir = os.path.join(group_dir, "device_ap_configs")
    os.makedirs(dev_dir, exist_ok=True)
    saved = 0

    for ap in aps:
        serial = ap.get("serial") or ap.get("serial_number", "")
        if not serial:
            continue
        resp = _ap_config.get_ap_config(central, serial)
        if resp["code"] != 200:
            continue
        cfg = resp["msg"]
        if cfg:
            _save(dev_dir, f"{serial}.json", {
                "serial":     serial,
                "name":       ap.get("name", ""),
                "cli_config": cfg,
            })
            saved += 1

    print(f"    device_ap_configs/  ({saved} of {len(aps)} APs had unique config)")
    return {"file": "device_ap_configs", "status": "ok",
            "detail": f"{saved}/{len(aps)} APs"}


def import_device_ap_configs(central, group_name: str, group_dir: str,
                               import_name: str = None) -> bool:
    """Re-push per-device CLI configs using ApConfiguration.replace_ap().

    Device serials are physical identifiers and are not affected by renames.
    APs must exist and be assigned to the group on the target before import.
    """
    dev_dir = os.path.join(group_dir, "device_ap_configs")
    if not os.path.isdir(dev_dir):
        return True

    files  = [f for f in os.listdir(dev_dir) if f.endswith(".json")]
    ok_all = True

    for fname in files:
        data    = _load(dev_dir, fname)
        serial  = data.get("serial", fname.replace(".json", ""))
        cfg     = data.get("cli_config", [])
        payload = {"clis": cfg} if isinstance(cfg, list) else cfg
        resp    = _ap_config.replace_ap(central, serial, payload)
        if resp["code"] in (200, 201):
            print(f"  [{group_name}] Device config pushed: {serial}")
        else:
            print(f"  [{group_name}] Device config failed: {serial} - {resp['code']}: {resp['msg']}")
            ok_all = False

    return ok_all


# ---------------------------------------------------------------------------
# 4. WLAN configurations  (IAP groups)
#
#    Step 1: Wlan.get_all_wlans(conn, group_name)
#            GET /configuration/v1/wlan/{group_name} - SSID summary list
#
#    Step 2: Wlan.get_wlan(conn, group_name, wlan_name)
#            GET /configuration/full_wlan/{group_name}/{wlan_name}
#            Full WLAN detail - requires full_wlan allowlisting.
#            Degrades gracefully to summary-only if not allowlisted.
# ---------------------------------------------------------------------------

def export_wlans(central, group_name: str, group_dir: str, **_):
    """Export all WLAN configurations for an IAP group.

    Saves:
        wlans_summary.json  - raw list from get_all_wlans
        wlans.json          - full detail per WLAN from get_wlan
    """
    list_resp = _wlan.get_all_wlans(central, group_name)
    if list_resp["code"] != 200:
        print(f"    wlans.json  [WARN: HTTP {list_resp['code']} - skipped]")
        return

    summary = list_resp["msg"]
    # Normalise WLAN list from all known response formats:
    #   {"wlans": [...]}           current Classic Central format
    #   {"data": [...]}            older clusters
    #   [...]                      bare list (rare)
    if isinstance(summary, list):
        wlan_list = summary
    elif isinstance(summary, dict):
        wlan_list = summary.get("wlans") or summary.get("data") or []
    else:
        wlan_list = []
    _save(group_dir, "wlans_summary.json", summary)

    wlans_detail = []
    skipped      = 0

    for entry in wlan_list:
        wlan_name = (
            entry.get("essid")
            or entry.get("wlan_name")
            or entry.get("name", "")
        )
        if not wlan_name:
            continue

        detail_resp = _wlan.get_wlan(central, group_name, wlan_name)
        if detail_resp["code"] == 200:
            wlans_detail.append(detail_resp["msg"])
        else:
            # full_wlan endpoint may not be allowlisted - degrade gracefully
            wlans_detail.append({"essid": wlan_name, "_summary_only": True, **entry})
            skipped += 1

    _save(group_dir, "wlans.json", wlans_detail)
    note = f", {skipped} summary-only (full_wlan not allowlisted)" if skipped else ""
    print(f"    wlans.json          ({len(wlans_detail)} WLANs{note})")
    print(f"    wlans_summary.json")
    return {"file": "wlans.json", "status": "ok" if not skipped else "warn",
            "detail": f"{len(wlans_detail)} WLANs{note}"}


def import_wlans(central, group_name: str, group_dir: str,
                  import_name: str = None) -> bool:
    """Re-create WLANs using Wlan.create_full_wlan() or Wlan.create_wlan().

    Groups with full detail use create_full_wlan (full_wlan endpoint).
    Groups with summary-only data use create_wlan with basic fields.
    """
    wlans = _load(group_dir, "wlans.json")
    if not wlans:
        return True

    target = import_name or group_name
    ok_all = True

    for wlan in wlans:
        wlan_name = (
            wlan.get("essid") or wlan.get("wlan_name") or wlan.get("name", "")
        )
        if not wlan_name:
            continue

        if wlan.get("_summary_only"):
            resp = _wlan.create_wlan(central, target, wlan_name, wlan)
        else:
            resp = _wlan.create_full_wlan(central, target, wlan_name, wlan)

        if resp["code"] in (200, 201):
            print(f"  [{group_name}] WLAN created: {wlan_name}")
        else:
            print(f"  [{group_name}] WLAN failed:  {wlan_name} - {resp['code']}: {resp['msg']}")
            ok_all = False

    return ok_all


# ---------------------------------------------------------------------------
# 5. AP Settings  (per-device, IAP groups)
#
#    Uses ApSettings.get_ap_settings(conn, serial)
#    GET /configuration/v2/ap_settings/{serial}
#    Captures hostname, zone name, radio channel/power overrides.
# ---------------------------------------------------------------------------

def export_ap_settings(central, group_name: str, group_dir: str, **_):
    """Export per-AP settings (hostname, zone, radio config) for all APs.

    Saves: ap_settings/<serial>.json
    """
    aps = _get_aps_in_group(central, group_name)
    if not aps:
        print(f"    ap_settings/        [no APs found - skipped]")
        return

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
    """Re-apply per-AP settings using ApSettings.update_ap_settings().

    APs must exist and be assigned to the target group before import.
    """
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
# 6. Country code  (IAP groups)
#    No classic module wrapper - direct conn.command() call.
# ---------------------------------------------------------------------------

def export_country(central, group_name: str, group_dir: str, **_):
    """Fetch the RF country code for the group.

    GET /configuration/v1/{group_name}/country

    Response variants:
      {"country": "US"}                  standard
      {"country_code": "US"}             some clusters
      "US"                               bare string (rare)
    """
    resp = central.command(
        apiMethod="GET",
        apiPath=f"/configuration/v1/{group_name}/country",
    )
    if resp["code"] != 200:
        print(f"    country.json        [WARN: HTTP {resp['code']}: {resp['msg']}]")
        return {"file": "country.json", "status": "warn",
                "detail": f"HTTP {resp['code']}"}

    msg = resp["msg"]
    # Normalise to {"country": "XX"} regardless of source format
    if isinstance(msg, str):
        country_data = {"country": msg.strip()}
    elif isinstance(msg, dict):
        code = msg.get("country") or msg.get("country_code") or msg.get("code", "")
        country_data = {"country": code}
    else:
        country_data = {"country": ""}

    _save(group_dir, "country.json", country_data)
    code = country_data.get("country", "unknown")
    print(f"    country.json        ({code})")
    return {"file": "country.json", "status": "ok", "detail": code}


def import_country(central, group_name: str, group_dir: str,
                    import_name: str = None) -> bool:
    """Re-apply country code using PUT /configuration/v1/country."""
    data = _load(group_dir, "country.json")
    if not data or not data.get("country"):
        return True
    target = import_name or group_name
    resp   = central.command(
        apiMethod="PUT",
        apiPath="/configuration/v1/country",
        apiData={"groups": [target], "country": data["country"]},
    )
    if resp["code"] in (200, 201):
        print(f"  [{group_name}] Country set: {data['country']}")
        return True
    print(f"  [{group_name}] Country failed - {resp['code']}: {resp['msg']}")
    return False


# ---------------------------------------------------------------------------
# 7. AP Inventory  (IAP groups)
#
#    Saves a compact JSON array of every AP in the group:
#    [{serial, model, name, ip}, ...]
#
#    Used as the source-of-truth for New Central import:
#      - new_central_importer.get_ap_models_from_export() reads it to
#        determine which device groups to create and which APs to assign.
#
#    Not used during Classic → Classic import (import_fn is a no-op).
# ---------------------------------------------------------------------------

def export_ap_inventory(central, group_name: str, group_dir: str, **_):
    """Save AP inventory metadata for all APs in the group.

    Calls GET /monitoring/v2/aps (same endpoint as the device-config and
    ap-settings exporters) and saves the fields needed for New Central
    device-group creation:
      serial  — device identifier
      model   — AP hardware model, e.g. "AP-635"
      name    — hostname as shown in Central
      ip      — management IP address

    Saves: ap_inventory.json
    """
    aps = _get_aps_in_group(central, group_name)
    if not aps:
        print(f"    ap_inventory.json   [no APs found]")
        return {"file": "ap_inventory.json", "status": "ok", "detail": "0 APs"}

    inventory = []
    for ap in aps:
        serial = ap.get("serial") or ap.get("serial_number", "")
        if not serial:
            continue
        inventory.append({
            "serial": serial,
            "model":  ap.get("model", ""),
            "name":   ap.get("name") or ap.get("hostname", ""),
            "ip":     ap.get("ip_address") or ap.get("ip", ""),
        })

    _save(group_dir, "ap_inventory.json", inventory)
    print(f"    ap_inventory.json   ({len(inventory)} APs)")
    return {"file": "ap_inventory.json", "status": "ok",
            "detail": f"{len(inventory)} APs"}


def import_ap_inventory(*_, **__) -> bool:
    """No-op — ap_inventory.json is a reference artifact for New Central import only."""
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_iap(allowed_types: list) -> bool:
    return "IAP" in (allowed_types or [])


# ---------------------------------------------------------------------------
# Exporter registry
# ---------------------------------------------------------------------------

EXPORTERS = [
    {
        "name":       "properties",
        "applies_to": None,
        "export_fn":  export_properties,
        "import_fn":  import_properties,
    },
    {
        "name":       "ap_cli_config",
        "applies_to": {"IAP"},
        "export_fn":  export_ap_cli_config,
        "import_fn":  import_ap_cli_config,
    },
    {
        "name":       "device_ap_configs",
        "applies_to": {"IAP"},
        "export_fn":  export_device_ap_configs,
        "import_fn":  import_device_ap_configs,
    },
    {
        "name":       "wlans",
        "applies_to": {"IAP"},
        "export_fn":  export_wlans,
        "import_fn":  import_wlans,
    },
    {
        "name":       "ap_inventory",
        "applies_to": {"IAP"},
        "export_fn":  export_ap_inventory,
        "import_fn":  import_ap_inventory,
        "export_only": True,   # reference artifact only; import_fn is a no-op
    },
    {
        "name":       "ap_settings",
        "applies_to": {"IAP"},
        "export_fn":  export_ap_settings,
        "import_fn":  import_ap_settings,
    },
    {
        "name":       "country",
        "applies_to": {"IAP"},
        "export_fn":  export_country,
        "import_fn":  import_country,
    },
    # Future exporters:
    # {
    #     "name":       "switch_acls",
    #     "applies_to": {"ArubaSwitch", "CX"},
    #     "export_fn":  export_switch_acls,
    #     "import_fn":  import_switch_acls,
    # },
]


# All known Classic Central strings that identify an IAP/AP group
_IAP_ALIASES = {
    # Classic Central pre-AOS10 (Instant) values
    "IAP", "iap",
    "Instant", "instant", "INSTANT",
    "AP", "ap",
    # Classic Central AOS10 values — AllowedDevTypes returns these strings
    # for AOS10 groups instead of "IAP"
    "AccessPoints", "accesspoints", "access_points",
    "Gateways", "gateways",
    "AOS10", "aos10", "AOS_10", "AOS_10X", "aos_10x",
}

def get_active_exporters(allowed_types: list) -> list:
    """Return exporters whose applies_to set intersects with allowed_types.

    applies_to={"IAP"} exporters are also activated by any value in
    _IAP_ALIASES so that different Classic Central API variants (e.g.
    'Instant', 'AP') correctly trigger the IAP exporter set.
    """
    allowed_set = set(allowed_types or [])

    # Expand: if any IAP alias is present, treat "IAP" as present too
    if allowed_set & _IAP_ALIASES:
        allowed_set.add("IAP")

    return [
        e for e in EXPORTERS
        if e["applies_to"] is None or allowed_set & e["applies_to"]
    ]
