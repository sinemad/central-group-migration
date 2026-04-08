"""
export_groups.py — CLI export script
Exports Classic Central UI group configurations (AOS10) to the exports/
directory. Uses pycentral.classic modules for all API calls.

Exported per IAP group:
  properties.json              Group allowed types, AOS10 flag, monitor flags
  ap_cli_config.json           Full group CLI config (ApConfiguration.get_ap_config)
  device_ap_configs/<serial>   Per-AP override configs (one file per AP)
  wlans.json                   Full WLAN detail (Wlan.get_all_wlans + get_wlan)
  wlans_summary.json           Raw WLAN list from get_all_wlans
  ap_settings/<serial>         Per-AP hostname, zone, radio settings
  country.json                 RF country code

Usage:
    python export_groups.py

Authentication:
    Set CENTRAL_BASE_URL and CENTRAL_TOKEN environment variables,
    or edit central_info directly below.
"""

import json
import os
from datetime import datetime

from pycentral.classic.base import ArubaCentralBase
from pycentral.classic.configuration import Groups
from exporters import get_active_exporters

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
central_info = {
    "base_url": os.environ.get(
        "CENTRAL_BASE_URL",
        "https://apigw-prod2.central.arubanetworks.com"
    ),
    "token": {
        "access_token": os.environ.get("CENTRAL_TOKEN", "<your-access-token>")
    },
}
central = ArubaCentralBase(central_info=central_info, ssl_verify=True)

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _normalise_group_name(item) -> str:
    """Coerce any Central API group name format to a plain string."""
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        return _normalise_group_name(item[0]) if item else ""
    if isinstance(item, dict):
        return item.get("group") or item.get("name") or str(item)
    return str(item)


def get_all_groups(central) -> list:
    g = Groups()
    all_groups, offset, limit = [], 0, 20
    while True:
        resp = g.get_groups(central, offset=offset, limit=limit)
        if resp["code"] != 200:
            raise RuntimeError(f"GET groups failed {resp['code']}: {resp['msg']}")
        raw  = resp["msg"].get("data", [])
        page = [_normalise_group_name(i) for i in raw if i]
        all_groups.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return all_groups


# Import the response parser from app.py so parsing logic is not duplicated.
# If running this script standalone, the parser is defined inline below.
try:
    from app import _parse_properties_response, _normalise_properties
except ImportError:
    def _normalise_properties(raw: dict) -> dict:
        FIELD_MAP = {
            "AllowedDevTypes": "allowed_types", "AOSVersion": "_aos_version",
            "MonitorOnlySwitch": "monitor_only_sw", "MonitorOnlyCX": "monitor_only_cx",
            "GwNetworkRole": "gw_role", "NewCentral": "cnx", "MicroBranchOnly": "microbranch",
        }
        out = {FIELD_MAP.get(k, k): v for k, v in raw.items()}
        if "_aos_version" in out:
            out["aos10"] = str(out.pop("_aos_version", "")).upper() in ("AOS10", "AOS_10")
        out.setdefault("aos10", False)
        out.setdefault("monitor_only_sw", False)
        out.setdefault("monitor_only_cx", False)
        return out

    def _parse_properties_response(msg) -> dict:
        if not isinstance(msg, dict):
            return {}
        first_val = next(iter(msg.values()), None) if msg else None
        if isinstance(first_val, dict) and "data" not in msg:
            return {g: _normalise_properties(r) for g, r in msg.items() if isinstance(r, dict)}
        result = {}
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            group = entry.get("group") or entry.get("group_name", "")
            if not group:
                continue
            if "properties" in entry and isinstance(entry["properties"], dict):
                result[group] = _normalise_properties(entry["properties"])
            else:
                result[group] = _normalise_properties(
                    {k: v for k, v in entry.items() if k not in ("group", "group_name")})
        return result


def get_group_properties(central, group_names: list) -> dict:
    props = {}
    for chunk in chunked(group_names, 20):
        resp = central.command(
            apiMethod="GET",
            apiPath="/configuration/v1/groups/properties",
            apiParams={"groups": ",".join(chunk)},
        )
        if resp["code"] == 200:
            props.update(_parse_properties_response(resp["msg"]))
    return props


def save_manifest(group_names: list, source_url: str):
    manifest = {
        "_exported_at":    datetime.utcnow().isoformat() + "Z",
        "_source_cluster": source_url,
        "groups":          group_names,
    }
    with open(os.path.join(EXPORT_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nSaved manifest ({len(group_names)} groups)")


# ---------------------------------------------------------------------------
# Run export
# ---------------------------------------------------------------------------

group_names = get_all_groups(central)
print(f"Found {len(group_names)} groups\n")

group_properties = get_group_properties(central, group_names)

for group_name in group_names:
    properties    = group_properties.get(group_name, {})
    allowed_types = properties.get("allowed_types", [])

    group_dir = os.path.join(EXPORT_DIR, group_name)
    os.makedirs(group_dir, exist_ok=True)

    print(f"{group_name}/  (types: {allowed_types})")

    for exporter in get_active_exporters(allowed_types):
        exporter["export_fn"](
            central=central,
            group_name=group_name,
            group_dir=group_dir,
            properties=properties,
        )

save_manifest(group_names, central_info["base_url"])
print("Export complete")
