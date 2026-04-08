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

app = Flask(__name__)
CORS(app)

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

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
        return json.load(f)


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
            props = _get_group_properties(conn, groups)

            _emit(q, "start", {"total": len(groups)})

            for group_name in groups:
                group_dir = os.path.join(EXPORT_DIR, group_name)
                os.makedirs(group_dir, exist_ok=True)
                properties  = props.get(group_name, {})
                allowed     = properties.get("allowed_types", [])
                exporters   = get_active_exporters(allowed)
                files       = []

                print(f"[export] {group_name}: allowed_types={allowed}, "
                      f"active_exporters={[e['name'] for e in exporters]}",
                      flush=True)

                for exporter in exporters:
                    try:
                        result = exporter["export_fn"](
                            central=conn,
                            group_name=group_name,
                            group_dir=group_dir,
                            properties=properties,
                        )
                        # export_fn returns a status dict or None
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
                    "group":        group_name,
                    "allowed_types": allowed,
                    "files":        files,
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


@app.route("/api/import", methods=["POST"])
def start_import():
    body = request.json or {}
    base_url = body.get("base_url", "").rstrip("/")
    token = body.get("token", "")
    selected = body.get("groups", [])
    if not base_url or not token:
        return jsonify({"ok": False, "error": "base_url and token required"}), 400

    p = os.path.join(EXPORT_DIR, "manifest.json")
    if not os.path.exists(p):
        return jsonify({"ok": False, "error": "No manifest found — run export first"}), 400

    with open(p) as f:
        manifest = json.load(f)

    op_id = f"import_{int(time.time()*1000)}"
    q: queue.Queue = queue.Queue()
    _progress_queues[op_id] = q

    def run():
        try:
            conn = _make_conn(base_url, token)
            all_groups = manifest.get("groups", [])
            groups = [g for g in all_groups if g in selected] if selected else all_groups

            # Resolve import names (original → renamed, or unchanged)
            rename_map = manifest.get("renames", {})
            import_names = {g: _import_name(manifest, g) for g in groups}

            try:
                existing = set(_get_all_groups(conn))
            except Exception:
                existing = set()

            # Skip if the *import* name (possibly renamed) already exists on target
            to_create = [g for g in groups if import_names[g] not in existing]
            skipped   = [g for g in groups if import_names[g] in existing]

            _emit(q, "start", {"total": len(to_create), "skipped": skipped})

            for group_name in to_create:
                iname     = import_names[group_name]
                group_dir = os.path.join(EXPORT_DIR, group_name)
                renamed   = iname != group_name

                if not os.path.isdir(group_dir):
                    _emit(q, "group_done", {
                        "group": group_name, "import_name": iname,
                        "renamed": renamed, "status": "missing", "steps": []
                    })
                    continue

                props   = _load(group_dir, "properties.json") or {}
                allowed = props.get("allowed_types", [])
                steps   = []

                ok = _import_properties(conn, group_name, group_dir, import_name=iname)
                steps.append({"name": "Create group", "ok": ok})

                if ok and _has_iap(allowed):
                    ok2 = _import_ap_cli_config(conn, group_name, group_dir, import_name=iname)
                    steps.append({"name": "CLI config", "ok": ok2})

                    ok3 = _import_country(conn, group_name, group_dir, import_name=iname)
                    steps.append({"name": "Country code", "ok": ok3})

                overall = all(s["ok"] for s in steps)
                _emit(q, "group_done", {
                    "group":       group_name,
                    "import_name": iname,
                    "renamed":     renamed,
                    "status":      "ok" if overall else "fail",
                    "steps":       steps
                })

            _emit(q, "complete", {"total": len(to_create), "skipped": len(skipped)})
        except Exception as e:
            _emit(q, "error", {"message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "op_id": op_id})


@app.route("/api/import/progress/<op_id>")
def import_progress(op_id):
    return Response(
        stream_with_context(_sse_stream(op_id)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)


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
        props    = _load(gdir, "properties.json") or {}
        country  = _load(gdir, "country.json") or {}
        # Enumerate all known exportable file/directory artefacts
        known_artefacts = [
            "properties.json",
            "ap_cli_config.json",
            "country.json",
            "wlans.json",
            "wlans_summary.json",
            "device_ap_configs",   # directory
            "ap_settings",         # directory
        ]
        present = []
        for art in known_artefacts:
            p = os.path.join(gdir, art)
            if os.path.exists(p):
                present.append(art)
        # Per-device counts
        n_device_configs = len([
            f for f in os.listdir(os.path.join(gdir, "device_ap_configs"))
            if f.endswith(".json")
        ]) if os.path.isdir(os.path.join(gdir, "device_ap_configs")) else 0
        n_ap_settings = len([
            f for f in os.listdir(os.path.join(gdir, "ap_settings"))
            if f.endswith(".json")
        ]) if os.path.isdir(os.path.join(gdir, "ap_settings")) else 0
        n_wlans = len(_load(gdir, "wlans.json") or [])

        groups.append({
            "name":             name,
            "import_name":      renames.get(name, name),
            "renamed":          name in renames,
            "allowed_types":    props.get("allowed_types", []),
            "aos10":            props.get("aos10", False),
            "monitor_only_sw":  props.get("monitor_only_sw", False),
            "monitor_only_cx":  props.get("monitor_only_cx", False),
            "country":          country.get("country", ""),
            "has_cli_config":   "ap_cli_config.json" in present,
            "n_wlans":          n_wlans,
            "n_device_configs": n_device_configs,
            "n_ap_settings":    n_ap_settings,
            "files":            present,
        })

    return jsonify({"ok": True, "manifest": manifest, "groups": groups})


@app.route("/api/groups/<group_name>")
def get_group(group_name):
    """Return full detail for a single exported group, including CLI config."""
    gdir = os.path.join(EXPORT_DIR, group_name)
    if not os.path.isdir(gdir):
        return jsonify({"ok": False,
                        "error": f"Group '{group_name}' not found on disk"}), 404

    manifest   = _read_manifest() or {}
    renames    = manifest.get("renames", {})
    props      = _load(gdir, "properties.json") or {}
    country    = _load(gdir, "country.json") or {}
    cli_raw    = _load(gdir, "ap_cli_config.json")
    cli_config = cli_raw.get("cli_config", []) if cli_raw else None

    # WLAN detail
    wlans      = _load(gdir, "wlans.json") or []

    # Per-device counts
    dev_dir  = os.path.join(gdir, "device_ap_configs")
    sett_dir = os.path.join(gdir, "ap_settings")
    device_configs = [
        f.replace(".json", "") for f in os.listdir(dev_dir)
        if f.endswith(".json")
    ] if os.path.isdir(dev_dir) else []
    ap_settings_serials = [
        f.replace(".json", "") for f in os.listdir(sett_dir)
        if f.endswith(".json")
    ] if os.path.isdir(sett_dir) else []

    return jsonify({
        "ok":                   True,
        "name":                 group_name,
        "import_name":          renames.get(group_name, group_name),
        "renamed":              group_name in renames,
        "properties":           props,
        "country":              country.get("country", ""),
        "cli_config":           cli_config,
        "wlans":                wlans,
        "n_wlans":              len(wlans),
        "device_config_serials": device_configs,
        "n_device_configs":     len(device_configs),
        "n_ap_settings":        len(ap_settings_serials),
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


@app.route("/health")
def health():
    return jsonify({"ok": True}), 200

