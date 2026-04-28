"""
Aruba Central Group Export/Import — Flask Backend
Uses pycentral as the API layer, consistent with the rest of the project.
"""

import json
import os
import queue
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_cors import CORS

from pycentral.classic.base import ArubaCentralBase
from pycentral.classic.configuration import Groups
from exporters import get_active_exporters
from new_central_importer import get_existing_sites, import_group_to_site, import_device_groups, assign_aps_to_site

app = Flask(__name__)
CORS(app)

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(__file__), "backups"))
os.makedirs(BACKUP_DIR, exist_ok=True)

_progress_queues: dict[str, queue.Queue] = {}


# ---------------------------------------------------------------------------
# pycentral connection factory
# ---------------------------------------------------------------------------

def _make_conn(base_url: str, token: str) -> ArubaCentralBase:
    """Return an ArubaCentralBase instance using an access token."""
    central_info = {
        "base_url": base_url,
        "token": {"access_token": token}
    }
    return ArubaCentralBase(central_info=central_info, token_store=None, ssl_verify=True)


# ---------------------------------------------------------------------------
# Core logic — mirrors exporters.py, all calls via conn.command()
# ---------------------------------------------------------------------------

def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _save(group_dir: str, filename: str, data):
    os.makedirs(group_dir, exist_ok=True)
    with open(os.path.join(group_dir, filename), "w") as f:
        json.dump(data, f, indent=2)


def _load(group_dir: str, filename: str):
    p = os.path.join(group_dir, filename)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        data = json.load(f)
    # Handle double-encoded JSON — file content is a JSON-serialised string
    # rather than a parsed object. Unwrap it transparently.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            pass
    return data


def _read_manifest() -> dict | None:
    """Load manifest.json from EXPORT_DIR, return None if absent."""
    p = os.path.join(EXPORT_DIR, "manifest.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _write_manifest(manifest: dict):
    """Atomically write manifest.json."""
    p = os.path.join(EXPORT_DIR, "manifest.json")
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)


def _import_name(manifest: dict, original: str) -> str:
    """Return the name that should be used on the target instance.

    If a rename has been set for *original*, returns the new name.
    Otherwise returns *original* unchanged.
    """
    return manifest.get("renames", {}).get(original, original)


def _central_error(code: int, msg) -> str:
    """Return a human-readable error string from a Central API response.

    Extracts Central's error_description when present, then always appends
    remediation guidance for known status codes (401, 403, 404) so the user
    knows exactly how to resolve the problem.
    """
    # Extract Central's own description if available
    central_desc = None
    if isinstance(msg, dict):
        central_desc = (
            msg.get("error_description")
            or msg.get("message")
            or msg.get("error")
        )

    REMEDIATION = {
        401: (
            "Token expired or invalid. "
            "Tokens are valid for 2 hours — generate a new one from Central: "
            "Maintain → Organization → Platform Integration → API Gateway → "
            "My Apps & Tokens → Generate Token."
        ),
        403: (
            "Access denied. "
            "The account does not have API access or lacks the required role. "
            "Minimum: Read-Only for export, Admin for import."
        ),
        404: (
            "Endpoint not found. "
            "Verify the Base URL is correct for your cluster. "
            "Example: https://apigw-prod2.central.arubanetworks.com"
        ),
    }

    if code in REMEDIATION:
        # Show Central's description first (what happened), then our fix (what to do)
        if central_desc and central_desc.lower() not in REMEDIATION[code].lower():
            return f"{central_desc} — {REMEDIATION[code]}"
        return REMEDIATION[code]

    if central_desc:
        return central_desc

    return f"Central API returned HTTP {code}. Response: {msg}"


def _normalise_group_name(item) -> str:
    """Coerce a single item from the Central groups API response to a plain string.

    The /configuration/v2/groups endpoint returns group names differently across
    Classic Central versions:

      - Current:   ["Group-A", "Group-B"]           plain strings
      - Some clusters: [["Group-A"], ["Group-B"]]   nested single-item lists
      - Some clusters: [{"group": "Group-A"}, ...]  dicts with a "group" key

    This function normalises all three formats to a plain string.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        # Nested list — take the first element and recurse in case it's also nested
        return _normalise_group_name(item[0]) if item else ""
    if isinstance(item, dict):
        # Dict format — "group" is the canonical key, fall back to "name"
        return item.get("group") or item.get("name") or str(item)
    return str(item)


def _get_all_groups(conn: ArubaCentralBase) -> list:
    g = Groups()
    all_groups, offset, limit = [], 0, 20
    while True:
        resp = g.get_groups(conn, offset=offset, limit=limit)
        if resp["code"] != 200:
            raise RuntimeError(_central_error(resp["code"], resp["msg"]))
        raw_page = resp["msg"].get("data", [])
        # Normalise each item to a plain string — handles all known Central
        # API response formats (string list, nested list, dict list)
        page = [_normalise_group_name(item) for item in raw_page if item]
        all_groups.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return all_groups


# Classic Central API uses CamelCase field names in the properties response.
# We normalise to the snake_case keys used throughout this project.
_PROP_FIELD_MAP = {
    "AllowedDevTypes":   "allowed_types",
    "AOSVersion":        "_aos_version",   # raw string; converted to bool below
    "MonitorOnlySwitch": "monitor_only_sw",
    "MonitorOnlyCX":     "monitor_only_cx",
    "GwNetworkRole":     "gw_role",
    "NewCentral":        "cnx",
    "MicroBranchOnly":   "microbranch",
}


def _normalise_properties(raw: dict) -> dict:
    """Convert a single group's raw API properties dict to our storage format.

    Handles CamelCase -> snake_case mapping and converts the AOSVersion string
    (e.g. 'AOS10') to a boolean 'aos10' field used by the rest of the code.
    Unknown fields are passed through as-is so nothing is silently dropped.
    """
    out = {}
    for k, v in raw.items():
        mapped = _PROP_FIELD_MAP.get(k)
        if mapped:
            out[mapped] = v
        else:
            out[k] = v

    # AOSVersion / Architecture → aos10 bool
    # Known AOS10 strings from Classic Central: AOS10, AOS_10, AOS_10X
    _AOS10_STRINGS = {"AOS10", "AOS_10", "AOS_10X", "AOS10X"}
    if "_aos_version" in out:
        raw_ver = str(out.pop("_aos_version", "")).upper().replace("-", "_")
        out["aos10"] = any(raw_ver.startswith(s) for s in _AOS10_STRINGS)
    # Also detect from the "Architecture" field some clusters include
    if not out.get("aos10") and "Architecture" in out:
        out["aos10"] = str(out.pop("Architecture", "")).upper() in _AOS10_STRINGS
    elif "Architecture" in out:
        out.pop("Architecture")          # already set via AOSVersion — remove duplicate
    out.setdefault("aos10", False)

    out.setdefault("monitor_only_sw", False)
    out.setdefault("monitor_only_cx", False)
    return out


def _parse_properties_response(msg) -> dict:
    """Parse the /configuration/v1/groups/properties response into a dict
    keyed by group name.

    Classic Central returns this endpoint in different formats depending on
    cluster version. All known formats are handled:

    Format A — dict keyed by group name (older clusters):
        {"Branch-APs": {"AllowedDevTypes": ["IAP"], ...}}

    Format B — list under "data" with nested "properties" key:
        {"data": [{"group": "Branch-APs", "properties": {...}}, ...]}

    Format C — list under "data" with flat properties at top level:
        {"data": [{"group": "Branch-APs", "AllowedDevTypes": [...], ...}]}

    Format D — bare list (no wrapper dict):
        [{"group": "Branch-APs", "properties": {...}}, ...]

    Format E — v3-style with "group_properties" key:
        {"data": [{"group": "Branch-APs", "group_properties": {...}}]}
    """
    # Format D: bare list
    if isinstance(msg, list):
        return _parse_properties_list(msg)

    if not isinstance(msg, dict):
        return {}

    # Format A: keys are group names (values are dicts, not lists/primitives)
    first_val = next(iter(msg.values()), None) if msg else None
    if isinstance(first_val, dict) and "data" not in msg:
        return {
            group: _normalise_properties(raw)
            for group, raw in msg.items()
            if isinstance(raw, dict)
        }

    # Formats B, C, E: list under "data" key
    data = msg.get("data", [])
    if isinstance(data, list) and data:
        return _parse_properties_list(data)

    return {}


def _parse_properties_list(data: list) -> dict:
    """Parse a list of group property entries into a group-keyed dict."""
    result = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        group = (entry.get("group")
                 or entry.get("group_name")
                 or entry.get("name", ""))
        if not group:
            continue
        # Format B/E: nested properties sub-dict
        nested = (entry.get("properties")
                  or entry.get("group_properties"))
        if isinstance(nested, dict):
            result[group] = _normalise_properties(nested)
        else:
            # Format C: flat — everything except identifier keys is a property
            raw = {k: v for k, v in entry.items()
                   if k not in ("group", "group_name", "name")}
            result[group] = _normalise_properties(raw)
    return result


def _get_group_properties(conn: ArubaCentralBase, group_names: list) -> dict:
    props = {}
    for chunk in _chunked(group_names, 20):
        resp = conn.command(
            apiMethod="GET",
            apiPath="/configuration/v1/groups/properties",
            apiParams={"groups": ",".join(chunk)}
        )
        if resp["code"] == 200:
            parsed = _parse_properties_response(resp["msg"])
            print(f"[props] chunk={chunk} raw_keys={list(resp['msg'].keys()) if isinstance(resp['msg'],dict) else type(resp['msg']).__name__} parsed={list(parsed.keys())}",
                  flush=True)
            for gname, gprops in parsed.items():
                print(f"[props]   {gname}: allowed_types={gprops.get('allowed_types')} aos10={gprops.get('aos10')}",
                      flush=True)
            props.update(parsed)
        elif resp["code"] in (401, 403):
            raise RuntimeError(_central_error(resp["code"], resp["msg"]))
        else:
            # Log non-200/non-auth failures so they're visible in docker logs
            print(f"[props] WARN HTTP {resp['code']} for groups={chunk}: {resp['msg']}",
                  flush=True)
    return props


def _export_properties(group_dir, properties):
    _save(group_dir, "properties.json", properties)
    return {"file": "properties.json", "status": "ok"}


def _export_ap_cli_config(conn: ArubaCentralBase, group_name: str, group_dir: str):
    from pycentral.classic.configuration import ApConfiguration
    ap_cfg = ApConfiguration()
    resp = ap_cfg.get_ap_config(conn, group_name)
    if resp["code"] != 200:
        return {"file": "ap_cli_config.json", "status": "warn",
                "detail": f"HTTP {resp['code']}"}
    _save(group_dir, "ap_cli_config.json", {"cli_config": resp["msg"]})
    lines = len(resp["msg"]) if isinstance(resp["msg"], list) else 0
    return {"file": "ap_cli_config.json", "status": "ok", "detail": f"{lines} CLI lines"}


def _export_country(conn: ArubaCentralBase, group_name: str, group_dir: str):
    resp = conn.command(
        apiMethod="GET",
        apiPath=f"/configuration/v1/{group_name}/country"
    )
    if resp["code"] != 200:
        return {"file": "country.json", "status": "warn",
                "detail": f"HTTP {resp['code']}"}
    _save(group_dir, "country.json", resp["msg"])
    return {"file": "country.json", "status": "ok",
            "detail": resp["msg"].get("country", "")}


def _import_properties(conn: ArubaCentralBase, group_name: str, group_dir: str,
                        import_name: str = None) -> bool:
    """Create the group on the target. Uses import_name as the created group name
    when a rename has been set; falls back to group_name (the original) otherwise."""
    props = _load(group_dir, "properties.json")
    if props is None:
        return False
    payload = {
        "group": import_name or group_name,
        "group_attributes": {
            "template_info": {"Wired": False, "Wireless": False},
            "group_properties": props
        }
    }
    resp = conn.command(
        apiMethod="POST",
        apiPath="/configuration/v3/groups",
        apiData=payload
    )
    return resp["code"] in (200, 201)


def _import_ap_cli_config(conn: ArubaCentralBase, group_name: str, group_dir: str,
                           import_name: str = None) -> bool:
    from pycentral.classic.configuration import ApConfiguration
    data = _load(group_dir, "ap_cli_config.json")
    if data is None:
        return True
    target  = import_name or group_name
    cli_cfg = data.get("cli_config", [])
    payload = {"clis": cli_cfg} if isinstance(cli_cfg, list) else cli_cfg
    ap_cfg  = ApConfiguration()
    resp    = ap_cfg.replace_ap(conn, target, payload)
    return resp["code"] in (200, 201)


def _import_country(conn: ArubaCentralBase, group_name: str, group_dir: str,
                     import_name: str = None) -> bool:
    cd = _load(group_dir, "country.json")
    if not cd or not cd.get("country"):
        return True
    target = import_name or group_name
    resp = conn.command(
        apiMethod="PUT",
        apiPath="/configuration/v1/country",
        apiData={"groups": [target], "country": cd["country"]}
    )
    return resp["code"] in (200, 201)


# Classic Central clusters return different strings for IAP device type
# across API versions. This set covers all known variants.
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

def _has_iap(allowed_types: list) -> bool:
    """Return True if any element of allowed_types identifies an IAP/AP group."""
    return bool(set(allowed_types or []) & _IAP_ALIASES)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _emit(q: queue.Queue, event: str, data: dict):
    q.put({"event": event, "data": data})


def _sse_stream(op_id: str):
    q = _progress_queues.get(op_id)
    if q is None:
        yield "data: {}\n\n"
        return
    while True:
        try:
            item = q.get(timeout=60)
            if item is None:
                yield f"event: done\ndata: {{}}\n\n"
                break
            event = item.get("event", "message")
            payload = json.dumps(item["data"])
            yield f"event: {event}\ndata: {payload}\n\n"
        except queue.Empty:
            yield ": keepalive\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/connect", methods=["POST"])
def connect():
    """Validate credentials and return group list."""
    body = request.json or {}
    base_url = body.get("base_url", "").rstrip("/")
    token = body.get("token", "")
    if not base_url or not token:
        return jsonify({"ok": False, "error": "base_url and token are required"}), 400
    try:
        conn = _make_conn(base_url, token)
        groups = _get_all_groups(conn)
        props = _get_group_properties(conn, groups)
        return jsonify({"ok": True, "groups": groups, "properties": props})
    except RuntimeError as e:
        msg = str(e)
        # Mirror the upstream HTTP status so the UI can colour the error correctly
        if "invalid or expired" in msg or "invalid_token" in msg:
            return jsonify({"ok": False, "error": msg, "code": 401}), 401
        if "Access denied" in msg:
            return jsonify({"ok": False, "error": msg, "code": 403}), 403
        return jsonify({"ok": False, "error": msg, "code": 500}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "code": 500}), 500


@app.route("/api/export", methods=["POST"])
def start_export():
    body = request.json or {}
    base_url = body.get("base_url", "").rstrip("/")
    token = body.get("token", "")
    selected = body.get("groups", [])
    if not base_url or not token:
        return jsonify({"ok": False, "error": "base_url and token required"}), 400

    op_id = f"export_{int(time.time()*1000)}"
    q: queue.Queue = queue.Queue()
    _progress_queues[op_id] = q

    def run():
        try:
            conn = _make_conn(base_url, token)
            all_groups = _get_all_groups(conn)
            groups = [g for g in all_groups if g in selected] if selected else all_groups
            exporters = get_active_exporters([])

            _emit(q, "start", {"total": len(groups)})

            for group_name in groups:
                group_dir = os.path.join(EXPORT_DIR, group_name)
                os.makedirs(group_dir, exist_ok=True)
                files = []

                print(f"[export] {group_name}", flush=True)

                for exporter in exporters:
                    try:
                        result = exporter["export_fn"](
                            central=conn,
                            group_name=group_name,
                            group_dir=group_dir,
                        )
                        if result:
                            files.append(result)
                            print(f"[export]   {exporter['name']}: "
                                  f"{result.get('status')} — {result.get('detail','')}",
                                  flush=True)
                    except Exception as exp_err:
                        import traceback
                        print(f"[export]   {exporter['name']}: EXCEPTION — {exp_err}",
                              flush=True)
                        traceback.print_exc()
                        files.append({
                            "file":   exporter["name"],
                            "status": "error",
                            "detail": str(exp_err),
                        })

                _emit(q, "group_done", {
                    "group": group_name,
                    "files": files,
                })

            manifest = {
                "_exported_at":    datetime.utcnow().isoformat() + "Z",
                "_source_cluster": base_url,
                "groups":          groups,
            }
            with open(os.path.join(EXPORT_DIR, "manifest.json"), "w") as f:
                json.dump(manifest, f, indent=2)

            _emit(q, "complete", {"total": len(groups)})
        except Exception as e:
            _emit(q, "error", {"message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})


@app.route("/api/export/progress/<op_id>")
def export_progress(op_id):
    return Response(
        stream_with_context(_sse_stream(op_id)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/api/manifest")
def get_manifest():
    """Return the manifest of the current export on disk."""
    p = os.path.join(EXPORT_DIR, "manifest.json")
    if not os.path.exists(p):
        return jsonify({"ok": False, "error": "No export found on disk"}), 404
    with open(p) as f:
        manifest = json.load(f)

    groups_detail = []
    for g in manifest.get("groups", []):
        gdir = os.path.join(EXPORT_DIR, g)
        files = []
        for fname in ["properties.json", "ap_cli_config.json", "country.json"]:
            if os.path.exists(os.path.join(gdir, fname)):
                files.append(fname)
        props = _load(gdir, "properties.json") or {}
        groups_detail.append({
            "name": g,
            "allowed_types": props.get("allowed_types", []),
            "files": files
        })

    return jsonify({"ok": True, "manifest": manifest, "groups": groups_detail})


@app.route("/api/import/new-central/sites", methods=["POST"])
def get_nc_sites():
    """Return all existing sites from a New Central instance, sorted by name."""
    body = request.json or {}
    base_url = body.get("base_url", "").rstrip("/")
    token = body.get("token", "")
    if not base_url or not token:
        return jsonify({"ok": False, "error": "base_url and token required"}), 400
    try:
        conn = _make_conn(base_url, token)
        sites_map = get_existing_sites(conn)
        sites = sorted(
            [{"site_id": sid, "site_name": name} for name, sid in sites_map.items()],
            key=lambda s: s["site_name"].lower(),
        )
        return jsonify({"ok": True, "sites": sites, "total": len(sites)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/import/new-central/memberships", methods=["POST"])
def get_ap_memberships():
    """Return current site and device-group membership for a list of AP serials.

    POST body: {base_url, token, serials: [serial, ...]}
    Response:  {ok: true, memberships: {serial: {site_name, device_group}}}

    Uses GET /monitoring/v2/aps (paginated) which returns site and group_name
    for every AP visible to the account.  Entries whose serial is not in the
    requested list are filtered out so the response stays compact.
    """
    body = request.json or {}
    base_url = body.get("base_url", "").rstrip("/")
    token    = body.get("token", "")
    want     = set(body.get("serials", []))

    if not base_url or not token:
        return jsonify({"ok": False, "error": "base_url and token required"}), 400

    try:
        conn = _make_conn(base_url, token)
        memberships: dict[str, dict] = {}
        offset, limit = 0, 100

        while True:
            resp = conn.command(
                apiMethod="GET",
                apiPath="/monitoring/v2/aps",
                apiParams={"offset": offset, "limit": limit,
                           "fields": "serial,site,group_name"},
            )
            if resp["code"] != 200:
                break
            msg   = resp.get("msg") or {}
            data  = msg.get("aps") or []
            total = msg.get("total", 0)

            for ap in data:
                s = ap.get("serial", "")
                if s and (not want or s in want):
                    memberships[s] = {
                        "site_name":    ap.get("site") or ap.get("site_name") or None,
                        "device_group": ap.get("group_name") or None,
                    }

            # Stop early once all requested serials are found
            if want and all(s in memberships for s in want):
                break
            if not data or offset + len(data) >= total:
                break
            offset += limit

        return jsonify({"ok": True, "memberships": memberships})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/import/new-central", methods=["POST"])
def start_import_new_central():
    """Assign APs from exported Classic Central groups to existing New Central sites.

    Accepts a mappings dict {group_name: site_id} — no sites are created.
    Only groups present in the mappings dict are processed.
    """
    body = request.json or {}
    base_url = body.get("base_url", "").rstrip("/")
    token = body.get("token", "")
    mappings = body.get("mappings", {})   # {group_name: site_id}
    verbose  = bool(body.get("verbose", False))
    if not base_url or not token:
        return jsonify({"ok": False, "error": "base_url and token required"}), 400
    if not mappings:
        return jsonify({"ok": False, "error": "No site mappings provided"}), 400

    op_id = f"ncimport_{int(time.time()*1000)}"
    q: queue.Queue = queue.Queue()
    _progress_queues[op_id] = q

    def vlog(msg: str, level: str = "debug"):
        """Emit a verbose log line only when verbose mode is on."""
        if verbose:
            _emit(q, "log", {"level": level, "message": msg})

    def run():
        try:
            conn = _make_conn(base_url, token)
            groups = list(mappings.keys())
            _emit(q, "start", {"total": len(groups)})

            if verbose:
                _emit(q, "log", {"level": "info",
                                 "message": f"Verbose logging enabled — target: {base_url}"})

            for group_name in groups:
                mapping_info = mappings[group_name]
                # Accept either {site_id, serials} dict or a plain site_id int/str
                if isinstance(mapping_info, dict):
                    site_id = int(mapping_info["site_id"])
                    explicit_serials = mapping_info.get("serials")  # None = use all
                else:
                    site_id = int(mapping_info)
                    explicit_serials = None

                vlog(f"── Group: {group_name!r}  →  site_id={site_id}")

                group_dir = os.path.join(EXPORT_DIR, group_name)

                if not os.path.isdir(group_dir):
                    vlog(f"  Export directory not found: {group_dir}", "warn")
                    _emit(q, "group_done", {
                        "group":          group_name,
                        "site_id":        site_id,
                        "status":         "missing",
                        "ap_count":       0,
                        "failed_serials": [],
                        "steps": [{"name": "Assign APs", "ok": False,
                                   "detail": "Export directory not found"}],
                    })
                    continue

                if explicit_serials is not None:
                    serials = [s for s in explicit_serials if s]
                    vlog(f"  Serial source: explicit selection ({len(serials)} APs)")
                else:
                    inventory = _load(group_dir, "ap_inventory.json") or []
                    serials = [e["serial"] for e in inventory if e.get("serial")]
                    vlog(f"  Serial source: ap_inventory.json ({len(inventory)} entries → {len(serials)} serials)")

                if serials:
                    vlog(f"  Serials: {', '.join(serials)}")

                if not serials:
                    vlog(f"  No APs to assign — skipping", "warn")
                    _emit(q, "group_done", {
                        "group":          group_name,
                        "site_id":        site_id,
                        "status":         "ok",
                        "ap_count":       0,
                        "failed_serials": [],
                        "steps": [{"name": "Assign APs", "ok": True,
                                   "detail": "No APs selected"}],
                    })
                    continue

                # Inline chunked assignment with per-chunk verbose logging
                from new_central_importer import _chunked, _CHUNK_SIZE
                ok_all = True
                failed: list[str] = []
                chunks = list(_chunked(serials, _CHUNK_SIZE))
                vlog(f"  Sending {len(serials)} APs in {len(chunks)} chunk(s) "
                     f"(chunk size={_CHUNK_SIZE})")

                for idx, chunk in enumerate(chunks, 1):
                    vlog(f"  Chunk {idx}/{len(chunks)}: POST /central/v2/sites/associations "
                         f"[{', '.join(chunk)}]")
                    resp = conn.command(
                        apiMethod="POST",
                        apiPath="/central/v2/sites/associations",
                        apiData={
                            "site_id":     site_id,
                            "device_ids":  chunk,
                            "device_type": "IAP",
                        },
                    )
                    code = resp.get("code")
                    msg  = resp.get("msg")
                    vlog(f"  Chunk {idx} response: HTTP {code}  body={str(msg)[:300]}")
                    if code not in (200, 201):
                        ok_all = False
                        failed.extend(chunk)
                        vlog(f"  Chunk {idx} FAILED — {len(chunk)} AP(s) not assigned", "error")
                    else:
                        vlog(f"  Chunk {idx} OK — {len(chunk)} AP(s) assigned")

                assigned = len(serials) - len(failed)
                if failed:
                    vlog(f"  Failed serials: {', '.join(failed)}", "warn")
                vlog(f"  Result: {assigned}/{len(serials)} assigned  ok={ok_all}")

                _emit(q, "group_done", {
                    "group":          group_name,
                    "site_id":        site_id,
                    "status":         "ok" if ok_all else "fail",
                    "ap_count":       len(serials),
                    "failed_serials": failed,
                    "steps": [{"name": "Assign APs", "ok": ok_all,
                               "detail": f"{assigned}/{len(serials)} assigned"}],
                })

            # --- Device group assignment (Aruba_<model>) ---
            _emit(q, "log", {"level": "info", "message": "── Device group assignment"})
            dg_models: dict[str, list[str]] = {}
            dg_seen: set[str] = set()
            for gn in groups:
                mi = mappings[gn]
                explicit = mi.get("serials") if isinstance(mi, dict) else None
                gdir = os.path.join(EXPORT_DIR, gn)
                if not os.path.isdir(gdir):
                    continue
                inv = _load(gdir, "ap_inventory.json") or []
                if explicit is not None:
                    ex_set = set(explicit)
                    inv = [e for e in inv if e.get("serial") in ex_set]
                for entry in inv:
                    serial = (entry.get("serial") or "").strip()
                    model  = (entry.get("model")  or "").strip()
                    if serial and model and serial not in dg_seen:
                        dg_seen.add(serial)
                        dg_models.setdefault(model, []).append(serial)

            dg_results: list[dict] = []
            if not dg_models:
                _emit(q, "log", {"level": "warn",
                                 "message": "  No AP model data — skipping device group step"})
            else:
                from new_central_importer import _chunked, _CHUNK_SIZE

                # Step 1: fetch all existing device groups once
                vlog("  Fetching existing New Central device groups…")
                existing_dg: set[str] = set()
                dg_list_offset = 0
                while True:
                    gl_resp = conn.command(
                        apiMethod="GET",
                        apiPath="/configuration/v2/groups",
                        apiParams={"offset": dg_list_offset, "limit": 100},
                    )
                    gl_code = gl_resp.get("code")
                    gl_msg  = gl_resp.get("msg")
                    vlog(f"  GET /configuration/v2/groups offset={dg_list_offset}: HTTP {gl_code}  raw={str(gl_msg)[:300]}")
                    if gl_code != 200:
                        break
                    # API may return a list directly or {"data": [...], "total": N}
                    if isinstance(gl_msg, list):
                        gl_data  = gl_msg
                        gl_total = len(gl_msg)
                    else:
                        gl_msg   = gl_msg or {}
                        gl_data  = gl_msg.get("data") or []
                        gl_total = gl_msg.get("total", len(gl_data))
                    for g in gl_data:
                        if isinstance(g, str):
                            existing_dg.add(g)
                        elif isinstance(g, list) and g:
                            # API wraps each name in a single-element list: ['Aruba_AP-515']
                            existing_dg.add(str(g[0]))
                        elif isinstance(g, dict):
                            name = g.get("group") or g.get("name") or g.get("group_name") or ""
                            if name:
                                existing_dg.add(name)
                    if not gl_data or dg_list_offset + len(gl_data) >= gl_total:
                        break
                    dg_list_offset += 100
                vlog(f"  Found {len(existing_dg)} existing group(s)")

                # Step 2: fetch current group membership for all APs we intend to move
                all_dg_serials = list(dg_seen)
                vlog(f"  Checking current group for {len(all_dg_serials)} AP(s)…")
                ap_current_group: dict[str, str] = {}  # serial → current group_name
                mem_offset = 0
                while True:
                    m_resp = conn.command(
                        apiMethod="GET",
                        apiPath="/monitoring/v2/aps",
                        apiParams={"offset": mem_offset, "limit": 100,
                                   "fields": "serial,group_name"},
                    )
                    m_code = m_resp.get("code")
                    m_msg  = m_resp.get("msg") or {}
                    if m_code != 200:
                        vlog(f"  GET /monitoring/v2/aps: HTTP {m_code} — skipping membership check", "warn")
                        break
                    m_data  = m_msg.get("aps") or []
                    m_total = m_msg.get("total", 0)
                    for ap in m_data:
                        s = ap.get("serial", "")
                        if s in dg_seen:
                            ap_current_group[s] = ap.get("group_name") or ""
                    if all(s in ap_current_group for s in all_dg_serials):
                        break  # found every serial we care about
                    if not m_data or mem_offset + len(m_data) >= m_total:
                        break
                    mem_offset += 100
                for s in all_dg_serials:
                    vlog(f"  AP {s} current group: {ap_current_group.get(s, '(unknown)')!r}")

                # Step 3: per-model create (if needed) + move (if not already member)
                for model, serials in sorted(dg_models.items()):
                    dg_name = f"Aruba_{model}"
                    vlog(f"  Model {model}: {len(serials)} AP(s) → device group {dg_name!r}")

                    # Create only if the group doesn't already exist
                    if dg_name in existing_dg:
                        vlog(f"  Group {dg_name!r} already exists — skipping creation")
                        group_ok = True
                    else:
                        cr_resp = conn.command(
                            apiMethod="POST",
                            apiPath="/configuration/v2/groups",
                            apiData={
                                "group": dg_name,
                                "group_attributes": {
                                    "template_info": {"Wired": False, "Wireless": True},
                                    "group_properties": {
                                        "AllowedDevTypes": ["AccessPoints"],
                                        "AOSVersion":      "AOS10",
                                        "NewCentral":      True,
                                    },
                                },
                            },
                        )
                        cr_code = cr_resp.get("code")
                        cr_body = cr_resp.get("msg") or {}
                        cr_desc = (cr_body.get("description") or "") if isinstance(cr_body, dict) else str(cr_body)
                        vlog(f"  Create group {dg_name!r}: HTTP {cr_code}  body={cr_desc[:200]}")
                        already_exists = "already exists" in cr_desc.lower()
                        group_ok = cr_code in (200, 201) or already_exists
                        if group_ok:
                            existing_dg.add(dg_name)

                    dg_failed: list[str] = []
                    assign_ok = True
                    if group_ok:
                        # Skip APs already in this group
                        to_move = [s for s in serials
                                   if ap_current_group.get(s) != dg_name]
                        already  = [s for s in serials
                                    if ap_current_group.get(s) == dg_name]
                        if already:
                            _emit(q, "log", {"level": "info",
                                             "message": f"  {len(already)} AP(s) already in {dg_name!r} — skipped: {already}"})
                        if to_move:
                            dg_chunks = list(_chunked(to_move, _CHUNK_SIZE))
                            for idx, chunk in enumerate(dg_chunks, 1):
                                vlog(f"  Move chunk {idx}/{len(dg_chunks)}: POST /configuration/v1/devices/move {chunk}")
                                mv_resp = conn.command(
                                    apiMethod="POST",
                                    apiPath="/configuration/v1/devices/move",
                                    apiData={"group": dg_name, "serials": chunk},
                                )
                                mv_code = mv_resp.get("code")
                                vlog(f"  Move chunk {idx} response: HTTP {mv_code}  body={str(mv_resp.get('msg',''))[:300]}")
                                if mv_code not in (200, 201):
                                    assign_ok = False
                                    dg_failed.extend(chunk)
                        else:
                            _emit(q, "log", {"level": "info",
                                             "message": f"  All APs already in {dg_name!r} — nothing to move"})
                    else:
                        assign_ok = False
                        dg_failed = list(serials)

                    overall = group_ok and assign_ok
                    moved = len(serials) - len(dg_failed)
                    _emit(q, "log", {
                        "level":   "info" if overall else "error",
                        "message": f"  {dg_name}: {moved}/{len(serials)} APs moved — {'OK' if overall else 'FAILED'}",
                    })
                    dg_results.append({
                        "model":          model,
                        "group_name":     dg_name,
                        "total":          len(serials),
                        "ok":             overall,
                        "failed_serials": dg_failed,
                    })

            _emit(q, "complete", {"total": len(groups), "dg_results": dg_results})
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            _emit(q, "error", {"message": str(e)})
            _emit(q, "log", {"level": "error", "message": f"Traceback:\n{tb}"})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})


@app.route("/api/import/new-central/progress/<op_id>")
def import_new_central_progress(op_id):
    return Response(
        stream_with_context(_sse_stream(op_id)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)


# ---------------------------------------------------------------------------
# Sample export — testing fixture
# ---------------------------------------------------------------------------

SAMPLE_CONFIG_FILE = os.path.join(EXPORT_DIR, ".sample_config.json")

_SAMPLE_DEFAULTS = {
    "enabled": False,
    "name": "SAMPLE-TEST-GROUP",
    "aps": [
        {"serial": "TEST-AP-0001", "name": "sample-ap-01", "model": "AP-635"},
    ],
}


def _read_sample_config() -> dict:
    if not os.path.exists(SAMPLE_CONFIG_FILE):
        return dict(_SAMPLE_DEFAULTS)
    with open(SAMPLE_CONFIG_FILE) as f:
        return json.load(f)


def _write_sample_files(name: str, aps: list):
    """Write ap_inventory.json, per-AP settings, and a stub CLI config for the sample group."""
    import shutil
    group_dir = os.path.join(EXPORT_DIR, name)
    sett_dir  = os.path.join(group_dir, "ap_settings")

    # Clear old ap_settings so stale serial files don't linger
    if os.path.isdir(sett_dir):
        shutil.rmtree(sett_dir)
    os.makedirs(sett_dir, exist_ok=True)

    inventory = [{"serial": ap["serial"], "name": ap["name"],
                  "model": ap["model"], "ip": ""} for ap in aps]
    _save(group_dir, "ap_inventory.json", inventory)

    for ap in aps:
        settings = {
            "achannel": "0", "atxpower": "-127",
            "dot11a_radio_disable": False, "dot11g_radio_disable": False,
            "gchannel": "0",  "gtxpower": "-127",
            "hostname": ap["name"], "ip_address": "0.0.0.0",
            "usb_port_disable": False, "zonename": "_#ALL#_",
        }
        with open(os.path.join(sett_dir, f"{ap['serial']}.json"), "w") as f:
            json.dump(settings, f, indent=2)

    stub_cli = {"cli_config": ["# Sample export — generated for import testing"]}
    _save(group_dir, "ap_cli_config.json", stub_cli)


@app.route("/api/sample")
def get_sample():
    return jsonify({"ok": True, **_read_sample_config()})


@app.route("/api/sample", methods=["POST"])
def save_sample():
    """Persist sample export config and write files to disk.

    Body: {enabled: bool, name: str, aps: [{serial, name, model}, ...]}

    Files are always written so the import pipeline can find them when enabled.
    The old directory is removed when the group name changes.
    """
    import shutil
    body    = request.json or {}
    enabled = bool(body.get("enabled", False))
    name    = (body.get("name") or "").strip()
    aps     = body.get("aps", [])

    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    if not (1 <= len(aps) <= 5):
        return jsonify({"ok": False, "error": "between 1 and 5 APs required"}), 400

    # Validate serials and names
    serials = [ap.get("serial", "").strip() for ap in aps]
    if any(not s for s in serials):
        return jsonify({"ok": False, "error": "All AP serial numbers are required"}), 400
    if len(set(serials)) != len(serials):
        return jsonify({"ok": False, "error": "AP serial numbers must be unique"}), 400

    # Conflict check: name must not collide with a real (non-sample) export group
    current = _read_sample_config()
    old_name = current.get("name", "")
    manifest = _read_manifest() or {}
    real_groups = [g for g in manifest.get("groups", []) if g != old_name]
    if name in real_groups:
        return jsonify({"ok": False,
                        "error": f"'{name}' is already used by a real export group"}), 409

    # Clean up old directory when name changes
    if old_name and old_name != name:
        old_dir = os.path.join(EXPORT_DIR, old_name)
        if os.path.isdir(old_dir):
            shutil.rmtree(old_dir)

    clean_aps = [{"serial": ap.get("serial","").strip(),
                  "name":   ap.get("name","").strip() or ap.get("serial","").strip(),
                  "model":  ap.get("model","").strip() or "AP-635"} for ap in aps]

    _write_sample_files(name, clean_aps)

    config = {"enabled": enabled, "name": name, "aps": clean_aps}
    with open(SAMPLE_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    return jsonify({"ok": True, **config})


@app.route("/api/groups")
def list_groups():
    """Return summary of all groups currently on disk."""
    p = os.path.join(EXPORT_DIR, "manifest.json")
    if not os.path.exists(p):
        return jsonify({"ok": False, "error": "No export on disk"}), 404

    with open(p) as f:
        manifest = json.load(f)

    renames = manifest.get("renames", {})
    groups = []
    for name in manifest.get("groups", []):
        gdir = os.path.join(EXPORT_DIR, name)
        inventory = _load(gdir, "ap_inventory.json") or []
        n_ap_settings = len([
            f for f in os.listdir(os.path.join(gdir, "ap_settings"))
            if f.endswith(".json")
        ]) if os.path.isdir(os.path.join(gdir, "ap_settings")) else 0
        present = [
            art for art in ("ap_inventory.json", "ap_cli_config.json", "ap_settings")
            if os.path.exists(os.path.join(gdir, art))
        ]
        groups.append({
            "name":          name,
            "import_name":   renames.get(name, name),
            "renamed":       name in renames,
            "n_aps":         len(inventory),
            "n_ap_settings": n_ap_settings,
            "files":         present,
        })

    # Inject sample group at the top if enabled (kept separate from real manifest)
    sample = _read_sample_config()
    if sample.get("enabled") and sample.get("name"):
        sname = sample["name"]
        if sname not in manifest.get("groups", []):
            sgdir = os.path.join(EXPORT_DIR, sname)
            sinventory = _load(sgdir, "ap_inventory.json") or []
            sn_sett = len([
                f for f in os.listdir(os.path.join(sgdir, "ap_settings"))
                if f.endswith(".json")
            ]) if os.path.isdir(os.path.join(sgdir, "ap_settings")) else 0
            groups.insert(0, {
                "name":          sname,
                "import_name":   sname,
                "renamed":       False,
                "n_aps":         len(sinventory),
                "n_ap_settings": sn_sett,
                "files":         ["ap_inventory.json", "ap_cli_config.json", "ap_settings"],
                "sample":        True,
            })

    return jsonify({"ok": True, "manifest": manifest, "groups": groups})


@app.route("/api/groups/<group_name>")
def get_group(group_name):
    """Return full detail for a single exported group, including CLI config."""
    gdir = os.path.join(EXPORT_DIR, group_name)
    if not os.path.isdir(gdir):
        return jsonify({"ok": False,
                        "error": f"Group '{group_name}' not found on disk"}), 404

    manifest  = _read_manifest() or {}
    renames   = manifest.get("renames", {})
    inventory = _load(gdir, "ap_inventory.json") or []

    # Load per-AP settings keyed by serial for fast lookup
    sett_dir = os.path.join(gdir, "ap_settings")
    ap_settings: dict[str, dict] = {}
    if os.path.isdir(sett_dir):
        for fname in sorted(os.listdir(sett_dir)):
            if fname.endswith(".json"):
                data = _load(sett_dir, fname)
                if data:
                    serial = fname[:-5]
                    ap_settings[serial] = data

    # Merge settings into inventory entries for the detail view
    aps = []
    for entry in inventory:
        serial = entry.get("serial", "")
        aps.append({**entry, "settings": ap_settings.get(serial)})

    cli_config = _load(gdir, "ap_cli_config.json")

    sample_cfg = _read_sample_config()
    is_sample  = (sample_cfg.get("enabled") and sample_cfg.get("name") == group_name)

    return jsonify({
        "ok":          True,
        "name":        group_name,
        "import_name": renames.get(group_name, group_name),
        "renamed":     group_name in renames,
        "aps":         aps,
        "n_aps":       len(aps),
        "n_settings":  len(ap_settings),
        "cli_config":  cli_config,
        "sample":      is_sample,
    })


@app.route("/api/groups/<group_name>/rename", methods=["PATCH"])
def rename_group(group_name):
    """Set or clear the import rename for a group.

    Body: {"new_name": "NewGroupName"}  — set rename
          {"new_name": ""}              — clear rename (restore original)

    The directory on disk is never renamed. The rename only affects what
    name is sent to Classic Central when the group is imported.
    """
    gdir = os.path.join(EXPORT_DIR, group_name)
    if not os.path.isdir(gdir):
        return jsonify({"ok": False,
                        "error": f"Group '{group_name}' not found on disk"}), 404

    body     = request.json or {}
    new_name = body.get("new_name", "").strip()

    # Validate — Central group names: max 32 single-byte ASCII, no spaces
    if new_name and new_name != group_name:
        if len(new_name) > 32:
            return jsonify({"ok": False,
                            "error": "Name must be 32 characters or fewer"}), 400
        if not new_name.isascii():
            return jsonify({"ok": False,
                            "error": "Name must contain ASCII characters only"}), 400
        if " " in new_name:
            return jsonify({"ok": False,
                            "error": "Name must not contain spaces"}), 400

    manifest = _read_manifest()
    if manifest is None:
        return jsonify({"ok": False, "error": "No manifest found on disk"}), 404

    renames = manifest.setdefault("renames", {})

    if not new_name or new_name == group_name:
        # Clear the rename
        renames.pop(group_name, None)
        if not renames:
            manifest.pop("renames", None)  # keep manifest clean when empty
        _write_manifest(manifest)
        return jsonify({"ok": True, "group": group_name,
                        "import_name": group_name, "renamed": False})

    # Check the new name isn't already used by another group's rename
    other_renames = {v for k, v in renames.items() if k != group_name}
    if new_name in other_renames:
        return jsonify({"ok": False,
                        "error": f"'{new_name}' is already used as a rename for another group"}), 409

    # Check it doesn't collide with another original group name
    if new_name in manifest.get("groups", []) and new_name != group_name:
        return jsonify({"ok": False,
                        "error": f"'{new_name}' is already an existing group name in this export"}), 409

    renames[group_name] = new_name
    _write_manifest(manifest)
    return jsonify({"ok": True, "group": group_name,
                    "import_name": new_name, "renamed": True})


@app.route("/api/debug/<group_name>")
def debug_group(group_name):
    """Diagnostic endpoint — shows raw API responses for a group.

    Requires query params: base_url and token.
    Returns raw responses from every API endpoint used for that group
    so mismatches between expected and actual response formats can be
    identified without modifying the export code.

    Usage: GET /api/debug/Branch-APs?base_url=https://...&token=xxx
    """
    base_url = request.args.get("base_url", "").rstrip("/")
    token    = request.args.get("token", "")
    if not base_url or not token:
        return jsonify({"error": "base_url and token query params required"}), 400

    try:
        conn = _make_conn(base_url, token)
        result = {}

        # 1. Raw properties response
        raw_props = conn.command(
            apiMethod="GET",
            apiPath="/configuration/v1/groups/properties",
            apiParams={"groups": group_name},
        )
        result["raw_properties_response"] = {
            "code": raw_props["code"],
            "msg":  raw_props["msg"],
        }

        # 2. Parsed properties (what we store in properties.json)
        parsed = _parse_properties_response(raw_props["msg"]) if raw_props["code"] == 200 else {}
        group_props = parsed.get(group_name, {})
        # Also try all keys in parsed (case-insensitive group name match)
        if not group_props and parsed:
            for k, v in parsed.items():
                if k.lower() == group_name.lower():
                    group_props = v
                    break
        result["parsed_properties"]   = group_props
        result["all_parsed_groups"]   = list(parsed.keys())  # show all group names found
        result["allowed_types"]       = group_props.get("allowed_types", [])
        result["active_exporters"]    = [e["name"] for e in get_active_exporters(
            group_props.get("allowed_types", []))]
        result["raw_properties_msg_type"] = type(raw_props["msg"]).__name__
        # Show the full raw msg structure for Format B/C detection
        if isinstance(raw_props["msg"], dict) and "data" in raw_props["msg"]:
            data = raw_props["msg"]["data"]
            result["raw_properties_data_sample"] = data[:2] if isinstance(data, list) else data

        # 3. AP CLI config raw response
        ap_cli = conn.command(
            apiMethod="GET",
            apiPath=f"/configuration/v1/ap_cli/{group_name}",
        )
        result["ap_cli_config"] = {
            "code":     ap_cli["code"],
            "msg_type": type(ap_cli["msg"]).__name__,
            "msg_len":  len(ap_cli["msg"]) if isinstance(ap_cli["msg"], (list, str)) else None,
            "msg_preview": str(ap_cli["msg"])[:200] if ap_cli["msg"] else None,
        }

        # 4. WLAN list raw response
        from pycentral.classic.configuration import Wlan
        wlan = Wlan()
        wlan_resp = wlan.get_all_wlans(conn, group_name)
        result["wlans"] = {
            "code":     wlan_resp["code"],
            "msg_type": type(wlan_resp["msg"]).__name__,
            "msg_keys": list(wlan_resp["msg"].keys()) if isinstance(wlan_resp["msg"], dict) else None,
            "msg_preview": str(wlan_resp["msg"])[:300],
        }

        # 5. Country raw response
        country_resp = conn.command(
            apiMethod="GET",
            apiPath=f"/configuration/v1/{group_name}/country",
        )
        result["country"] = {
            "code": country_resp["code"],
            "msg":  country_resp["msg"],
        }

        # 6. Monitoring APs (first page only)
        aps_resp = conn.command(
            apiMethod="GET",
            apiPath="/monitoring/v2/aps",
            apiParams={"group": group_name, "limit": 5, "offset": 0},
        )
        result["monitoring_aps"] = {
            "code":     aps_resp["code"],
            "msg_type": type(aps_resp["msg"]).__name__,
            "msg_keys": list(aps_resp["msg"].keys()) if isinstance(aps_resp["msg"], dict) else None,
            "total":    aps_resp["msg"].get("total") if isinstance(aps_resp["msg"], dict) else None,
            "msg_preview": str(aps_resp["msg"])[:300],
        }

        return jsonify({"ok": True, "group": group_name, "diagnostics": result})

    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e),
                        "traceback": traceback.format_exc()}), 500


LABELS_FILE = os.path.join(EXPORT_DIR, "labels.json")


def _read_labels() -> dict:
    if not os.path.exists(LABELS_FILE):
        return {"definitions": [], "assignments": {}}
    with open(LABELS_FILE) as f:
        return json.load(f)


def _write_labels(data: dict):
    with open(LABELS_FILE, "w") as f:
        json.dump(data, f, indent=2)


@app.route("/api/labels")
def get_labels():
    return jsonify({"ok": True, **_read_labels()})


@app.route("/api/labels", methods=["POST"])
def create_label():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    data = _read_labels()
    if name not in data["definitions"]:
        data["definitions"].append(name)
        _write_labels(data)
    return jsonify({"ok": True, "definitions": data["definitions"]})


@app.route("/api/labels/<name>", methods=["DELETE"])
def delete_label(name):
    data = _read_labels()
    data["definitions"] = [d for d in data["definitions"] if d != name]
    data["assignments"] = {
        g: [l for l in labels if l != name]
        for g, labels in data["assignments"].items()
    }
    _write_labels(data)
    return jsonify({"ok": True, "definitions": data["definitions"],
                    "assignments": data["assignments"]})


@app.route("/api/groups/<group_name>/labels", methods=["PUT"])
def set_group_labels(group_name):
    labels = (request.json or {}).get("labels", [])
    if not isinstance(labels, list):
        return jsonify({"ok": False, "error": "labels must be a list"}), 400
    data = _read_labels()
    known = set(data["definitions"])
    labels = [l for l in labels if l in known]
    if labels:
        data["assignments"][group_name] = labels
    else:
        data["assignments"].pop(group_name, None)
    _write_labels(data)
    return jsonify({"ok": True, "labels": data["assignments"].get(group_name, [])})


@app.route("/api/backups")
def list_backups():
    backups = []
    try:
        for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not fname.endswith(".tar.gz"):
                continue
            p = os.path.join(BACKUP_DIR, fname)
            stat = os.stat(p)
            backups.append({
                "filename": fname,
                "size": stat.st_size,
                "created_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
            })
    except OSError:
        pass
    return jsonify({"ok": True, "backups": backups, "backup_dir": BACKUP_DIR})


@app.route("/api/backup", methods=["POST"])
def create_backup():
    import tarfile
    if not os.path.exists(os.path.join(EXPORT_DIR, "manifest.json")):
        return jsonify({"ok": False, "error": "No export found to back up — run an export first"}), 400

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = f"exports_{ts}.tar.gz"
    dest = os.path.join(BACKUP_DIR, filename)

    with tarfile.open(dest, "w:gz") as tar:
        tar.add(EXPORT_DIR, arcname="exports")

    size = os.path.getsize(dest)
    return jsonify({"ok": True, "filename": filename, "size": size})


@app.route("/api/restore", methods=["POST"])
def restore_backup():
    import tarfile
    import shutil
    body = request.json or {}
    filename = os.path.basename(body.get("filename", ""))
    if not filename or not filename.endswith(".tar.gz"):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    src = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(src):
        return jsonify({"ok": False, "error": "Backup not found"}), 404

    real_export = os.path.realpath(EXPORT_DIR)

    for item in os.listdir(EXPORT_DIR):
        p = os.path.join(EXPORT_DIR, item)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)

    with tarfile.open(src, "r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            member.name = parts[1]
            target = os.path.realpath(os.path.join(real_export, member.name))
            if not target.startswith(real_export + os.sep) and target != real_export:
                continue
            tar.extract(member, EXPORT_DIR)

    return jsonify({"ok": True, "filename": filename})


@app.route("/api/backups/<filename>", methods=["DELETE"])
def delete_backup(filename):
    filename = os.path.basename(filename)
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "Backup not found"}), 404
    os.remove(path)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"ok": True}), 200

