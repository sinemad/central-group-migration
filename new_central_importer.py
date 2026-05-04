"""
new_central_importer.py
=======================
Import logic for migrating Classic Central groups to Aruba New Central.

Classic Central model:  APs live in Groups
New Central model:      APs are assigned to Sites

Workflow per exported group
---------------------------
1. Resolve the site name  (import_name from manifest rename, or original group name)
2. Skip if a site with that name already exists on the target
3. Create the site        POST /central/v2/sites
4. Collect AP serials     from ap_settings/ and device_ap_configs/ in the export
5. Assign APs to site     POST /central/v2/sites/associations

Only AP assignment is handled in this first phase.
Additional New Central object types (device groups, WLAN profiles, etc.)
will be added as separate steps once this baseline is validated.

New Central API surface used
-----------------------------
  GET  /central/v2/sites                 list existing sites (paginated)
  POST /central/v2/sites                 create a site
  POST /central/v2/sites/associations    bulk-assign APs to a site

All calls go through ArubaCentralBase.command() so the same token-based
auth and retry handling used everywhere else in the project applies here.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from pycentral.classic.base import ArubaCentralBase


# ---------------------------------------------------------------------------
# Site inventory
# ---------------------------------------------------------------------------

def get_existing_sites(conn: ArubaCentralBase) -> dict[str, int]:
    """Return {site_name: site_id} for every site on the New Central instance.

    Handles pagination and the two known response envelope variants:
      {"sites": [...], "total": N}   — current New Central format
      {"data":  [...], "total": N}   — some clusters / older firmware

    Each site entry is expected to contain:
      site_name  (str)   canonical name key
      site_id    (int)   numeric site identifier
    """
    sites: dict[str, int] = {}
    offset, limit = 0, 100

    while True:
        resp = conn.command(
            apiMethod="GET",
            apiPath="/central/v2/sites",
            apiParams={"offset": offset, "limit": limit},
        )
        if resp["code"] != 200:
            raise RuntimeError(
                f"GET /central/v2/sites failed — HTTP {resp['code']}: {resp['msg']}"
            )

        msg  = resp["msg"]
        data = msg.get("sites") or msg.get("data") or []
        total = msg.get("total", 0)

        for site in data:
            name = site.get("site_name") or site.get("name", "")
            sid  = site.get("site_id") or site.get("id")
            if name and sid is not None:
                sites[name] = int(sid)

        if not data or offset + len(data) >= total:
            break
        offset += limit

    return sites


# ---------------------------------------------------------------------------
# Site creation
# ---------------------------------------------------------------------------

def create_site(conn: ArubaCentralBase, site_name: str) -> Optional[int]:
    """Create a New Central site and return its site_id.

    Returns None if the API call fails.

    POST /central/v2/sites
    Body: {"site_name": "<name>", "site_address": {}}

    site_address is intentionally left empty — it is optional in New Central
    and can be set later via the UI.  Including an empty dict satisfies the
    schema without requiring address data that was not captured during export.
    """
    resp = conn.command(
        apiMethod="POST",
        apiPath="/central/v2/sites",
        apiData={"site_name": site_name, "site_address": {}},
    )

    if resp["code"] not in (200, 201):
        return None

    msg = resp["msg"]
    # New Central returns {"site_id": <int>, ...} on success
    site_id = msg.get("site_id") or msg.get("id")
    return int(site_id) if site_id is not None else None


# ---------------------------------------------------------------------------
# AP serial collection
# ---------------------------------------------------------------------------

def get_ap_serials_from_export(group_dir: str) -> list[str]:
    """Return all AP serial numbers found in the export directory.

    Two subdirectories are checked and their results are de-duplicated:

      ap_settings/        — created by export_ap_settings(); one file per AP
      device_ap_configs/  — created by export_device_ap_configs(); only APs
                            with per-device CLI overrides (a subset of all APs)

    ap_settings/ is the more complete source because it captures every AP
    in the group, not just those with configuration overrides.  Both are
    checked so that partial exports (e.g. only device configs were captured)
    still yield the correct serial list.

    File names are "<serial>.json" so stripping the extension gives the serial.
    """
    serials: set[str] = set()

    for subdir in ("ap_settings", "device_ap_configs"):
        d = os.path.join(group_dir, subdir)
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if fname.endswith(".json"):
                serials.add(fname[:-5])

    return sorted(serials)


# ---------------------------------------------------------------------------
# AP-to-site assignment
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 50  # New Central site association endpoint batch limit


def _chunked(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def assign_aps_to_site(
    conn: ArubaCentralBase,
    site_id: int,
    serials: list[str],
) -> tuple[bool, list[str]]:
    """Assign AP serials to a New Central site.

    Serials are sent in batches of up to _CHUNK_SIZE to stay within API limits.

    POST /central/v2/sites/associations
    Body: {
        "site_id":     <int>,
        "device_ids":  [<serial>, ...],
        "device_type": "IAP"
    }

    Returns (overall_ok: bool, failed_serials: list[str]).
    failed_serials will be empty on full success.
    """
    if not serials:
        return True, []

    ok_all       = True
    failed: list[str] = []

    for chunk in _chunked(serials, _CHUNK_SIZE):
        resp = conn.command(
            apiMethod="POST",
            apiPath="/central/v2/sites/associations",
            apiData={
                "site_id":     site_id,
                "device_ids":  chunk,
                "device_type": "IAP",
            },
        )
        if resp["code"] not in (200, 201):
            ok_all = False
            failed.extend(chunk)

    return ok_all, failed


# ---------------------------------------------------------------------------
# Per-group import orchestrator
# ---------------------------------------------------------------------------

def import_group_to_site(
    conn: ArubaCentralBase,
    group_name: str,
    group_dir: str,
    site_name: str,
    existing_sites: dict[str, int],
) -> dict:
    """Run the full Classic-group → New-Central-site import for one group.

    Returns a result dict:
    {
        "site_name":    str,
        "site_id":      int | None,
        "skipped":      bool,   # True if site already existed
        "site_ok":      bool,
        "ap_serials":   list[str],
        "assign_ok":    bool,
        "failed_serials": list[str],
        "steps":        [{"name": str, "ok": bool, "detail": str}, ...]
    }
    """
    steps: list[dict] = []
    result = {
        "site_name":      site_name,
        "site_id":        None,
        "skipped":        False,
        "site_ok":        False,
        "ap_serials":     [],
        "assign_ok":      False,
        "failed_serials": [],
        "steps":          steps,
    }

    # --- Step 1: resolve or create site ---
    if site_name in existing_sites:
        site_id = existing_sites[site_name]
        result["site_id"]  = site_id
        result["skipped"]  = True
        result["site_ok"]  = True
        steps.append({
            "name":   "Create site",
            "ok":     True,
            "detail": f"Already exists (site_id={site_id}) — skipped creation",
        })
    else:
        site_id = create_site(conn, site_name)
        if site_id is None:
            steps.append({"name": "Create site", "ok": False,
                          "detail": "API call failed — see server logs"})
            return result
        result["site_id"] = site_id
        result["site_ok"] = True
        steps.append({
            "name":   "Create site",
            "ok":     True,
            "detail": f"site_id={site_id}",
        })

    # --- Step 2: collect AP serials ---
    serials = get_ap_serials_from_export(group_dir)
    result["ap_serials"] = serials

    if not serials:
        steps.append({
            "name":   "Assign APs",
            "ok":     True,
            "detail": "No APs found in export — nothing to assign",
        })
        result["assign_ok"] = True
        return result

    # --- Step 3: assign APs to site ---
    ok, failed = assign_aps_to_site(conn, site_id, serials)
    result["assign_ok"]      = ok
    result["failed_serials"] = failed

    detail = f"{len(serials) - len(failed)}/{len(serials)} APs assigned"
    if failed:
        detail += f"; failed: {failed}"

    steps.append({"name": "Assign APs", "ok": ok, "detail": detail})
    return result


# ---------------------------------------------------------------------------
# AP model inventory
# ---------------------------------------------------------------------------

def get_ap_models_from_export(group_dir: str) -> dict[str, list[str]]:
    """Return {model: [serial, ...]} from ap_inventory.json in an export directory.

    ap_inventory.json is written by the ap_inventory exporter added to
    exporters.py.  Older exports that pre-date this exporter will have no
    such file; an empty dict is returned so callers degrade gracefully.

    Only entries with both a non-empty model and serial are included — APs
    that didn't report a model string to the Classic Central monitoring API
    are silently skipped.
    """
    p = os.path.join(group_dir, "ap_inventory.json")
    if not os.path.exists(p):
        return {}

    with open(p) as f:
        inventory = json.load(f)

    models: dict[str, list[str]] = {}
    for entry in inventory:
        model  = (entry.get("model") or "").strip()
        serial = (entry.get("serial") or "").strip()
        if model and serial:
            models.setdefault(model, []).append(serial)
    return models


# ---------------------------------------------------------------------------
# New Central device group creation
# ---------------------------------------------------------------------------

def create_device_group(conn: ArubaCentralBase, group_name: str) -> bool:
    """Create a New Central AP configuration device group.

    POST /configuration/v2/groups
    The group is created as a New Central (CNX), AP-only, AOS10 group.

    Returns True on success or if the group already exists (HTTP 409).
    Returns False on any other failure.

    New Central device groups differ from Classic groups in that
    ``NewCentral: true`` is set in group_properties, which marks them as
    managed by the New Central configuration engine.
    """
    payload = {
        "group": group_name,
        "group_attributes": {
            "template_info": {"Wired": False, "Wireless": True},
            "group_properties": {
                "AllowedDevTypes": ["AccessPoints"],
                "AOSVersion":      "AOS10",
                "NewCentral":      True,
            },
        },
    }
    resp = conn.command(
        apiMethod="POST",
        apiPath="/configuration/v2/groups",
        apiData=payload,
    )
    # 200/201 = created, 409 = already exists — both are acceptable
    return resp["code"] in (200, 201, 409)


def assign_aps_to_device_group(
    conn: ArubaCentralBase,
    group_name: str,
    serials: list[str],
) -> tuple[bool, list[str]]:
    """Move APs into a New Central device group.

    POST /device_management/v1/group/move
    Body: {"group": <name>, "serials": [...]}

    Devices are moved in batches of _CHUNK_SIZE.
    Returns (overall_ok: bool, failed_serials: list[str]).
    """
    if not serials:
        return True, []

    ok_all       = True
    failed: list[str] = []

    for chunk in _chunked(serials, _CHUNK_SIZE):
        resp = conn.command(
            apiMethod="POST",
            apiPath="/configuration/v1/devices/move",
            apiData={"group": group_name, "serials": chunk},
        )
        if resp["code"] not in (200, 201):
            ok_all = False
            failed.extend(chunk)

    return ok_all, failed


# ---------------------------------------------------------------------------
# Device group import orchestrator
# ---------------------------------------------------------------------------

def import_device_groups(
    conn: ArubaCentralBase,
    export_dir: str,
    groups: list[str],
) -> dict:
    """Create one New Central device group per unique AP model across all selected groups.

    Workflow
    --------
    1. Read ap_inventory.json from each group's export directory.
    2. Aggregate serials by model across all groups — e.g. both Branch-East
       and Branch-West may contain AP-635s.
    3. For each unique model:
       a. Create a New Central device group named after the model.
       b. Move all APs of that model into the group.

    The model name from Classic Central's monitoring API is used directly as
    the New Central device group name (e.g. "AP-635", "AP-515").

    Returns
    -------
    {
        "models": {
            "AP-635": {
                "ok": bool,
                "serials": [str, ...],
                "failed_serials": [str, ...]
            },
            ...
        },
        "total_aps":  int,
        "ok_count":   int,
        "fail_count": int,
    }

    Groups whose ap_inventory.json is absent (older exports that pre-date the
    ap_inventory exporter) are silently skipped.
    """
    # Aggregate model → serials across all selected groups, de-duplicating
    all_models: dict[str, list[str]] = {}
    seen: set[str] = set()
    for group_name in groups:
        group_dir = os.path.join(export_dir, group_name)
        for model, serials in get_ap_models_from_export(group_dir).items():
            for serial in serials:
                if serial not in seen:
                    seen.add(serial)
                    all_models.setdefault(model, []).append(serial)

    result: dict = {
        "models":     {},
        "total_aps":  sum(len(s) for s in all_models.values()),
        "ok_count":   0,
        "fail_count": 0,
    }

    for model, serials in sorted(all_models.items()):
        group_name = f"Aruba_AP-{_normalize_ap_model(model)}"
        group_ok = create_device_group(conn, group_name)

        if group_ok:
            assign_ok, failed = assign_aps_to_device_group(conn, group_name, serials)
        else:
            assign_ok, failed = False, list(serials)

        overall = group_ok and assign_ok
        result["models"][model] = {
            "ok":             overall,
            "group_name":     group_name,
            "serials":        serials,
            "failed_serials": failed,
        }
        if overall:
            result["ok_count"] += 1
        else:
            result["fail_count"] += 1

    return result


# ---------------------------------------------------------------------------
# Model normalisation
# ---------------------------------------------------------------------------

def _normalize_ap_model(model: str) -> str:
    """Return the bare model number with any leading 'AP-' prefix stripped.

    Classic Central may return the model as '515' or 'AP-515' depending on
    the firmware version.  Normalising to the bare number lets us build
    consistent device group names regardless of the source format.

    Examples: 'AP-515' → '515',  '515' → '515',  'AP-505H' → '505H'
    """
    m = model.strip()
    if m.upper().startswith("AP-"):
        return m[3:]
    return m


# ---------------------------------------------------------------------------
# 2.4 GHz AP-515 identification
# ---------------------------------------------------------------------------

def get_24ghz_ap515_serials(group_dir: str, serials: list[str]) -> list[str]:
    """Return serials from *serials* whose 2.4 GHz radio is enabled.

    Reads ap_settings/<serial>.json and returns serials where
    dot11g_radio_disable is explicitly False.  Serials with no settings
    file, or where the field is absent or True, are excluded so that only
    confirmed 2.4 GHz-enabled APs are routed to the special group.
    """
    result = []
    sett_dir = os.path.join(group_dir, "ap_settings")
    for serial in serials:
        p = os.path.join(sett_dir, f"{serial}.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                settings = json.load(f)
            if settings.get("dot11g_radio_disable") is False:
                result.append(serial)
        except Exception:
            pass
    return result