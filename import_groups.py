"""
import_groups.py — CLI import script
Reads the exports/ directory produced by export_groups.py and
re-creates all groups on a target Classic Central instance.

Import order per group:
  1. Create group (properties.json)
  2. Push group CLI config (ap_cli_config.json)
  3. Create WLANs (wlans.json)
  4. Set country code (country.json)
  5. Push per-device CLI configs (device_ap_configs/)  — APs must exist first
  6. Apply per-AP settings (ap_settings/)              — APs must exist first

Rename support: if manifest.json contains a "renames" key, each group is
created under its renamed import name on the target instance. The exports/
directory structure always uses the original group name.

Usage:
    python import_groups.py

Authentication:
    Set CENTRAL_BASE_URL and CENTRAL_TOKEN environment variables,
    or edit central_info directly below.
"""

import json
import os

from pycentral.classic.base import ArubaCentralBase
from pycentral.classic.configuration import Groups
from exporters import get_active_exporters

# ---------------------------------------------------------------------------
# Auth — target cluster
# ---------------------------------------------------------------------------
central_info = {
    "base_url": os.environ.get(
        "CENTRAL_BASE_URL",
        "https://apigw-prod2.central.arubanetworks.com"
    ),
    "token": {
        "access_token": os.environ.get("CENTRAL_TOKEN", "<target-access-token>")
    },
}
central = ArubaCentralBase(central_info=central_info, ssl_verify=True)

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    with open(os.path.join(EXPORT_DIR, "manifest.json")) as f:
        return json.load(f)


def load_properties(group_name: str) -> dict:
    p = os.path.join(EXPORT_DIR, group_name, "properties.json")
    if not os.path.exists(p):
        return {}
    with open(p) as f:
        return json.load(f)


def _normalise_group_name(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        return _normalise_group_name(item[0]) if item else ""
    if isinstance(item, dict):
        return item.get("group") or item.get("name") or str(item)
    return str(item)


def get_existing_groups(central) -> set:
    g = Groups()
    existing, offset, limit = set(), 0, 20
    while True:
        resp = g.get_groups(central, offset=offset, limit=limit)
        if resp["code"] != 200:
            raise RuntimeError(f"GET groups failed: {resp}")
        raw  = resp["msg"].get("data", [])
        page = [_normalise_group_name(i) for i in raw if i]
        existing.update(page)
        if len(page) < limit:
            break
        offset += limit
    return existing


# ---------------------------------------------------------------------------
# Load manifest and rename map
# ---------------------------------------------------------------------------

manifest    = load_manifest()
group_names = manifest["groups"]
renames     = manifest.get("renames", {})

print(f"Manifest loaded: {len(group_names)} groups")
print(f"Source cluster:  {manifest['_source_cluster']}")
print(f"Exported at:     {manifest['_exported_at']}")
if renames:
    print(f"Renames set:     {len(renames)}")
    for orig, new in renames.items():
        print(f"  {orig} → {new}")
print()

# ---------------------------------------------------------------------------
# Skip groups whose import name already exists on the target
# ---------------------------------------------------------------------------

existing = get_existing_groups(central)

groups_to_create, skipped = [], []
for g in group_names:
    import_name = renames.get(g, g)
    if import_name in existing:
        skipped.append((g, import_name))
    else:
        groups_to_create.append(g)

if skipped:
    print(f"Skipping {len(skipped)} group(s) already on target:")
    for orig, iname in skipped:
        print(f"  {orig} → {iname}" if iname != orig else f"  {orig}")
    print()

# ---------------------------------------------------------------------------
# Re-import in manifest order
# ---------------------------------------------------------------------------

results = {"created": [], "failed": [], "missing": []}

for group_name in groups_to_create:
    import_name = renames.get(group_name, group_name)
    renamed     = import_name != group_name
    label       = f"{group_name} → {import_name}" if renamed else group_name

    group_dir = os.path.join(EXPORT_DIR, group_name)
    if not os.path.isdir(group_dir):
        print(f"[{label}] Directory not found — skipped")
        results["missing"].append(group_name)
        continue

    properties    = load_properties(group_name)
    allowed_types = properties.get("allowed_types", [])

    print(f"{label}/  (types: {allowed_types})")
    group_ok = True

    for exporter in get_active_exporters(allowed_types):
        ok = exporter["import_fn"](
            central=central,
            group_name=group_name,
            group_dir=group_dir,
            import_name=import_name,
        )
        if not ok:
            group_ok = False

    results["created" if group_ok else "failed"].append(group_name)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\nImport complete:")
print(f"  Created: {len(results['created'])}")
print(f"  Failed:  {len(results['failed'])}")
print(f"  Missing: {len(results['missing'])}")
if results["failed"]:
    print(f"  Failed groups:  {results['failed']}")
if results["missing"]:
    print(f"  Missing groups: {results['missing']}")
