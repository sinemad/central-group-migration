"""
classic_restorer.py
===================
Disaster Recovery — restore APs from a Classic Central export back to their
original groups and sites.

Assumes the target Classic Central instance still has the groups and sites
intact.  Missing groups/sites are flagged during validation so the admin can
decide whether to proceed.

API endpoints used
------------------
  GET  /central/v2/sites                    list existing sites (paginated)
  POST /configuration/v1/devices/move       move APs to a Classic Central group
  POST /central/v2/sites/associations       assign APs to a site
"""

from __future__ import annotations

import json
import os

from pycentral.classic.base import ArubaCentralBase

_CHUNK_SIZE = 50


def _chunked(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ---------------------------------------------------------------------------
# Classic Central site inventory
# ---------------------------------------------------------------------------

def get_classic_sites(conn: ArubaCentralBase) -> dict[str, int]:
    """Return {site_name: site_id} for all sites in Classic Central.

    Uses the same /central/v2/sites endpoint as the New Central importer —
    the site API surface is identical in both environments.
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


# ---------------------------------------------------------------------------
# Export data helpers
# ---------------------------------------------------------------------------

def load_inventory(group_dir: str) -> list[dict]:
    """Return the AP inventory list from ap_inventory.json, or [] if absent."""
    p = os.path.join(group_dir, "ap_inventory.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pre-restore validation
# ---------------------------------------------------------------------------

def validate_dr_target(
    export_dir: str,
    group_names: list[str],
    classic_groups: set[str],
    classic_sites: dict[str, int],
) -> dict:
    """Check that every export group and its APs' sites exist in Classic Central.

    Returns
    -------
    {
        "groups": {
            <name>: {
                "exists":        bool,     # group present in Classic Central
                "ap_count":      int,
                "sites":         [str],    # unique non-empty site names
                "missing_sites": [str],    # sites absent from Classic Central
            }
        },
        "missing_groups": [str],
        "missing_sites":  [str],           # unique across all groups
    }
    """
    result: dict = {
        "groups":         {},
        "missing_groups": [],
        "missing_sites":  [],
    }
    all_missing_sites: set[str] = set()

    for name in group_names:
        gdir      = os.path.join(export_dir, name)
        inventory = load_inventory(gdir)

        sites_in_group = {e["site"] for e in inventory if e.get("site")}
        missing_sites  = [s for s in sites_in_group if s not in classic_sites]

        result["groups"][name] = {
            "exists":        name in classic_groups,
            "ap_count":      len(inventory),
            "sites":         sorted(sites_in_group),
            "missing_sites": missing_sites,
        }
        if name not in classic_groups:
            result["missing_groups"].append(name)
        all_missing_sites.update(missing_sites)

    result["missing_sites"] = sorted(all_missing_sites)
    return result


# ---------------------------------------------------------------------------
# Classic Central group assignment
# ---------------------------------------------------------------------------

def move_aps_to_group(
    conn: ArubaCentralBase,
    group_name: str,
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
            apiData={"group": group_name, "serials": chunk},
        )
        if resp["code"] not in (200, 201):
            ok_all = False
            failed.extend(chunk)

    return ok_all, failed


# ---------------------------------------------------------------------------
# Classic Central site assignment
# ---------------------------------------------------------------------------

def assign_aps_to_classic_site(
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
# Per-group restore orchestrator
# ---------------------------------------------------------------------------

def restore_group(
    conn: ArubaCentralBase,
    group_name: str,
    group_dir: str,
    classic_sites: dict[str, int],
) -> dict:
    """Restore one group: move APs to their original group then re-assign to sites.

    Returns
    -------
    {
        "group":        str,
        "ap_count":     int,
        "group_ok":     bool,
        "group_failed": [str],
        "site_results": [
            {"site": str, "ok": bool, "ap_count": int, "failed": [str]},
            ...
        ],
        "overall_ok":   bool,
    }
    """
    inventory = load_inventory(group_dir)
    serials   = [e["serial"] for e in inventory if e.get("serial")]

    result: dict = {
        "group":        group_name,
        "ap_count":     len(serials),
        "group_ok":     True,
        "group_failed": [],
        "site_results": [],
        "overall_ok":   True,
    }

    if not serials:
        return result

    # Step 1: move APs back to their Classic Central group
    group_ok, group_failed = move_aps_to_group(conn, group_name, serials)
    result["group_ok"]     = group_ok
    result["group_failed"] = group_failed
    if not group_ok:
        result["overall_ok"] = False

    # Step 2: assign each AP back to its original site
    site_to_serials: dict[str, list[str]] = {}
    for entry in inventory:
        serial = entry.get("serial", "")
        site   = entry.get("site", "")
        if serial and site:
            site_to_serials.setdefault(site, []).append(serial)

    for site_name in sorted(site_to_serials):
        site_serials = site_to_serials[site_name]
        site_id      = classic_sites.get(site_name)

        if site_id is None:
            result["site_results"].append({
                "site":     site_name,
                "ok":       False,
                "ap_count": len(site_serials),
                "failed":   site_serials,
                "error":    "Site not found in Classic Central",
            })
            result["overall_ok"] = False
            continue

        ok, failed = assign_aps_to_classic_site(conn, site_id, site_serials)
        result["site_results"].append({
            "site":     site_name,
            "ok":       ok,
            "ap_count": len(site_serials),
            "failed":   failed,
        })
        if not ok:
            result["overall_ok"] = False

    return result
