"""
classic_importer.py
===================
Forward migration — import APs from an exported Classic Central tenant
into a *different* Classic Central tenant.

Unlike the DR restorer (which assumes same-name groups/sites exist in the same
tenant), this module accepts explicit group and site mappings so names can
differ between source and target.

API endpoints used
------------------
  GET  /configuration/v2/groups              list existing groups (paginated)
  GET  /central/v2/sites                     list existing sites (paginated)
  POST /configuration/v1/devices/move        move APs to a Classic Central group
  POST /central/v2/sites/associations        assign APs to a site
"""

from __future__ import annotations

import json
import os

from pycentral.classic.base import ArubaCentralBase
from pycentral.classic.configuration import Groups

_CHUNK_SIZE = 50


def _chunked(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ---------------------------------------------------------------------------
# Target inventory — groups and sites
# ---------------------------------------------------------------------------

def get_classic_groups(conn: ArubaCentralBase) -> list[str]:
    """Return a sorted list of group names from a Classic Central instance."""
    g = Groups()
    all_groups: list[str] = []
    offset, limit = 0, 20

    while True:
        resp = g.get_groups(conn, offset=offset, limit=limit)
        if resp["code"] != 200:
            raise RuntimeError(
                f"GET /configuration/v2/groups HTTP {resp['code']}: {resp['msg']}"
            )
        raw = resp["msg"].get("data", [])
        page = [_normalise(item) for item in raw if item]
        all_groups.extend(page)
        if len(page) < limit:
            break
        offset += limit

    return sorted(set(all_groups))


def get_classic_sites(conn: ArubaCentralBase) -> dict[str, int]:
    """Return {site_name: site_id} for all sites in a Classic Central instance."""
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
                f"GET /central/v2/sites HTTP {resp['code']}: {resp['msg']}"
            )
        msg   = resp["msg"]
        data  = msg.get("sites") or msg.get("data") or []
        total = msg.get("total", 0)

        for site in data:
            name = site.get("site_name") or site.get("name", "")
            sid  = site.get("site_id")  or site.get("id")
            if name and sid is not None:
                sites[name] = int(sid)

        if not data or offset + len(data) >= total:
            break
        offset += limit

    return sites


def _normalise(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        return _normalise(item[0]) if item else ""
    if isinstance(item, dict):
        return item.get("group") or item.get("name") or str(item)
    return str(item)


# ---------------------------------------------------------------------------
# AP inventory helper
# ---------------------------------------------------------------------------

def load_inventory(group_dir: str) -> list[dict]:
    p = os.path.join(group_dir, "ap_inventory.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Group and site assignment
# ---------------------------------------------------------------------------

def move_aps_to_group(
    conn: ArubaCentralBase,
    target_group: str,
    serials: list[str],
) -> tuple[bool, list[str]]:
    """Move APs to a Classic Central group via POST /configuration/v1/devices/move."""
    if not serials:
        return True, []

    ok_all = True
    failed: list[str] = []

    for chunk in _chunked(serials, _CHUNK_SIZE):
        resp = conn.command(
            apiMethod="POST",
            apiPath="/configuration/v1/devices/move",
            apiData={"group": target_group, "serials": chunk},
        )
        if resp["code"] not in (200, 201):
            ok_all = False
            failed.extend(chunk)

    return ok_all, failed


def assign_aps_to_site(
    conn: ArubaCentralBase,
    site_id: int,
    serials: list[str],
) -> tuple[bool, list[str]]:
    """Assign APs to a Classic Central site via POST /central/v2/sites/associations."""
    if not serials:
        return True, []

    ok_all = True
    failed: list[str] = []

    for chunk in _chunked(serials, _CHUNK_SIZE):
        resp = conn.command(
            apiMethod="POST",
            apiPath="/central/v2/sites/associations",
            apiData={"site_id": site_id, "device_ids": chunk, "device_type": "IAP"},
        )
        if resp["code"] not in (200, 201):
            ok_all = False
            failed.extend(chunk)

    return ok_all, failed


# ---------------------------------------------------------------------------
# Per-group import orchestrator
# ---------------------------------------------------------------------------

def import_group(
    conn: ArubaCentralBase,
    export_group_name: str,
    target_group_name: str,
    group_dir: str,
    site_mapping: dict[str, str],   # {source_site_name: target_site_name}
    classic_sites: dict[str, int],  # {target_site_name: site_id}
    selected_serials: set[str] | None = None,
) -> dict:
    """Import one group into Classic Central with explicit group and site mappings.

    Parameters
    ----------
    export_group_name   Name of the group in the export (for logging).
    target_group_name   Name of the group in the target Classic Central.
    group_dir           Path to the export directory for this group.
    site_mapping        Maps source site names to target site names.
                        Source sites not in the mapping are skipped.
    classic_sites       Site name → ID lookup from the target Classic Central.
    selected_serials    If provided, only import these serials. None = all.

    Returns
    -------
    {
        "group":            str,
        "target_group":     str,
        "ap_count":         int,
        "group_ok":         bool,
        "group_failed":     [str],
        "site_results":     [{site, target_site, ok, ap_count, failed}],
        "skipped_sites":    [str],   # source sites with no mapping
        "overall_ok":       bool,
    }
    """
    inventory = load_inventory(group_dir)

    if selected_serials is not None:
        inventory = [e for e in inventory if e.get("serial") in selected_serials]

    serials = [e["serial"] for e in inventory if e.get("serial")]

    result: dict = {
        "group":         export_group_name,
        "target_group":  target_group_name,
        "ap_count":      len(serials),
        "group_ok":      True,
        "group_failed":  [],
        "site_results":  [],
        "skipped_sites": [],
        "overall_ok":    True,
    }

    if not serials:
        return result

    # Phase 1 — move APs to the target group
    group_ok, group_failed = move_aps_to_group(conn, target_group_name, serials)
    result["group_ok"]    = group_ok
    result["group_failed"] = group_failed
    if not group_ok:
        result["overall_ok"] = False

    # Phase 2 — assign APs to their mapped target sites
    source_site_to_serials: dict[str, list[str]] = {}
    for entry in inventory:
        serial      = entry.get("serial", "")
        source_site = entry.get("site", "")
        if serial and source_site:
            source_site_to_serials.setdefault(source_site, []).append(serial)

    for source_site, site_serials in sorted(source_site_to_serials.items()):
        target_site = site_mapping.get(source_site)
        if not target_site:
            result["skipped_sites"].append(source_site)
            continue

        site_id = classic_sites.get(target_site)
        if site_id is None:
            result["site_results"].append({
                "site":        source_site,
                "target_site": target_site,
                "ok":          False,
                "ap_count":    len(site_serials),
                "failed":      site_serials,
                "error":       f"Site '{target_site}' not found in target Classic Central",
            })
            result["overall_ok"] = False
            continue

        ok, failed = assign_aps_to_site(conn, site_id, site_serials)
        result["site_results"].append({
            "site":        source_site,
            "target_site": target_site,
            "ok":          ok,
            "ap_count":    len(site_serials),
            "failed":      failed,
        })
        if not ok:
            result["overall_ok"] = False

    return result
