# HPE Aruba Central Group Migration Tool
## Full Technical Documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Dependencies and Package Verification](#3-dependencies-and-package-verification)
4. [Docker Deployment](#4-docker-deployment)
5. [Local Development Setup](#5-local-development-setup)
6. [Authentication](#6-authentication)
7. [Web UI Guide](#7-web-ui-guide)
8. [CLI Scripts Guide](#8-cli-scripts-guide)
9. [Export File Format](#9-export-file-format)
10. [New Central API Surface](#10-new-central-api-surface)
11. [Flask HTTP API Reference](#11-flask-http-api-reference)
12. [Troubleshooting](#12-troubleshooting)
13. [Extending the Tool](#13-extending-the-tool)

---

## 1. Overview

This tool migrates access point configuration from an HPE Aruba **Classic
Central** tenant to **New Central**. It is designed specifically for
**AOS10 UI groups** — groups where device configuration is managed through
the Central web interface rather than CLI templates.

### Migration model

| Classic Central | New Central |
|-----------------|-------------|
| Group | Site (location) + Device group (config profile) |
| AP in a group | AP assigned to a site AND moved to a model-based device group |

The tool exports group configuration and AP inventory from Classic Central
to disk, then uses a visual mapping workflow to assign those APs to the
correct sites and device groups in New Central.

### What is exported per group

| File | Contents | Condition |
|------|----------|-----------|
| `properties.json` | Allowed device types, AOS10 flag, monitor-only flags | All groups |
| `ap_cli_config.json` | Full AOS10 CLI configuration blob | IAP groups |
| `country.json` | RF country code | IAP groups |
| `ap_inventory.json` | Serial, model, hostname, IP for each AP | IAP groups |
| `ap_settings/<serial>.json` | Per-AP hostname/radio settings | IAP groups |

### What is not in scope (first phase)

- Template groups
- WLAN profiles, RF profiles, security policies (separate export steps,
  to be added in future phases)
- Non-AP device types (switches, controllers)
- Firmware compliance policies

---

## 2. Architecture

```
central-migration/
├── app.py                   # Flask backend — all routes, SSE, orchestration
├── exporters.py             # Per-data-type export/import registry
├── new_central_importer.py  # New Central site and device group helpers
├── export_groups.py         # Standalone CLI export script
├── import_groups.py         # Standalone CLI import script
├── templates/
│   └── index.html           # Single-file web UI (Export / Import / Data tabs)
├── exports/                 # Runtime data — bind-mounted as Docker volume
│   ├── manifest.json
│   ├── .sample_config.json  # Sample export config (if enabled)
│   └── <group-name>/
│       ├── properties.json
│       ├── ap_cli_config.json
│       ├── ap_inventory.json
│       └── ap_settings/
│           └── <serial>.json
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

### Request flow

```
Browser (HTTP + SSE)
  │
  ▼
Flask / gunicorn  (1 worker, 8 threads)
  │
  ├── GET  /                              Serve index.html
  ├── POST /api/connect                   Test creds, return groups
  ├── POST /api/export                    Start background export thread
  ├── GET  /api/export/progress/<id>      SSE stream
  ├── GET  /api/groups                    List exports on disk
  ├── GET  /api/groups/<name>             Group detail (APs, CLI config)
  ├── POST /api/import                    Start background Classic import thread
  ├── GET  /api/import/progress/<id>      SSE stream
  ├── POST /api/import/new-central        Start background New Central import
  ├── GET  /api/import/new-central/progress/<id>  SSE stream
  ├── POST /api/import/new-central/sites          Fetch NC sites
  ├── POST /api/import/new-central/memberships    Check AP site/group membership
  ├── GET  /api/sample                    Read sample export config
  ├── POST /api/sample                    Save sample export config
  └── GET  /health                        Health check
```

### Why single gunicorn worker

Export and import operations push progress events into `_progress_queues`,
an in-process Python dict. SSE subscriber threads read from this same dict.
Multiple workers each have isolated memory — an export started in worker A
is invisible to an SSE subscriber connected to worker B, causing the
progress stream to hang silently. **One worker with eight threads is the
required configuration.**

---

## 3. Dependencies and Package Verification

### requirements.txt

```
pycentral>=2.0a17
flask==3.1.0
flask-cors==5.0.0
gunicorn==23.0.0
```

`pycentral >= 2.0a17` (pre-release) is required for the
`pycentral.classic` module used by this tool.

### Verifying inside a running container

```bash
docker exec central-migration python -c "
import importlib.metadata
for pkg in ['flask', 'flask-cors', 'gunicorn']:
    print(pkg, importlib.metadata.version(pkg))
from pycentral.classic.base import ArubaCentralBase
print('pycentral.classic OK')
"
```

---

## 4. Docker Deployment

### Prerequisites

- Docker Engine 24.0 or later
- Docker Compose v2 (`docker compose`, not `docker-compose`)
- Network access to the HPE Aruba Central API Gateway from the Docker host

### Build and start

```bash
docker compose up -d
```

On first run, `docker compose up` triggers a full image build. To watch
the build output:

```bash
docker compose build --progress=plain
```

### Common commands

```bash
# View live logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build

# Stop (exports/ data preserved on host)
docker compose down

# Stop and remove bind-mount data
docker compose down -v

# Check container health
docker inspect central-migration --format='{{.State.Health.Status}}'
```

### Exported data location

```yaml
# docker-compose.yml
volumes:
  - ./exports:/app/exports
```

All exported group data is written to `./exports/` on the host and persists
across container restarts and image rebuilds.

### Changing the host port

```yaml
# docker-compose.yml
ports:
  - "9090:8000"   # host port 9090 → container port 8000
```

### Reverse proxy (nginx)

```nginx
location / {
    proxy_pass         http://localhost:8000;
    proxy_http_version 1.1;
    proxy_set_header   Connection '';
    proxy_buffering    off;        # required for SSE
    proxy_cache        off;
    proxy_read_timeout 180s;
}
```

---

## 5. Local Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                    # Dev server at http://localhost:5000
```

Flask's built-in reloader restarts the server automatically when any `.py`
file is saved. Do not use the dev server in production.

### Running the CLI scripts locally

```bash
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<token> \
python export_groups.py

CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<new-tenant-token> \
python import_groups.py
```

---

## 6. Authentication

### Access token

```python
central_info = {
    "base_url": "https://apigw-prod2.central.arubanetworks.com",
    "token": {"access_token": "<api-gateway-access-token>"}
}
conn = ArubaCentralBase(central_info=central_info, token_store=None, ssl_verify=True)
```

Tokens are valid for 2 hours. Generate from **Maintain → Organization →
Platform Integration → API Gateway → My Apps & Tokens → Generate Token**.

### Cluster base URLs

| Region | Base URL |
|--------|----------|
| US-1 | `https://apigw-prod2.central.arubanetworks.com` |
| US-2 | `https://apigw-prod2-eu.central.arubanetworks.com` |
| EU-1 | `https://eu-apigw.central.arubanetworks.com` |
| APAC-1 | `https://apigw-apac.central.arubanetworks.com` |

### OAuth credentials (auto-refresh)

For operations longer than 2 hours, use OAuth credentials so tokens refresh
automatically:

```python
central_info = {
    "base_url":      "<base-url>",
    "username":      "<central-username>",
    "password":      "<central-password>",
    "client_id":     "<api-gateway-client-id>",
    "client_secret": "<api-gateway-client-secret>",
    "customer_id":   "<customer-id>"
}
```

The customer ID is shown in the top-right corner of the Central UI
(person icon → account info).

---

## 7. Web UI Guide

### Export tab

**Purpose:** Connect to Classic Central, select groups, write configuration
to disk.

1. Enter **Base URL** and **Access Token** for the Classic Central source
2. Click **Connect & Load Groups**
3. Select groups using checkboxes (**All** / **None** shortcuts available)
4. Click **↓ Export Selected**

Progress streams in real time. Result cards show which files were written
per group. Status meanings:

| Status | Meaning |
|--------|---------|
| `OK` | All files written |
| `WARN` | One file returned non-200 (e.g. no country code set) — rest OK |
| `FAIL` | Group properties fetch failed |

---

### Import tab

**Purpose:** Assign APs from the exported Classic Central groups to sites
and device groups in New Central.

#### Loading the export

Click **⬡ Load Export from Disk**. The sidebar populates from
`manifest.json`. Groups default to **no selection** — select the groups
you want to import.

#### Connecting to New Central

Enter the New Central **Base URL** and **Access Token**, then click
**Connect to New Central**. The tool fetches all existing sites and
auto-matches them to exported groups by exact name (case-sensitive).

Auto-matched groups show a green **✓ auto** badge. Unmatched groups can be
assigned manually from the site dropdown.

#### Site mapping

The mapping panel shows only the **selected** groups (those checked in the
left sidebar). Groups with no site mapping are skipped during import.

#### AP selection and membership pre-check

Click the **▶** expand arrow next to any group to view its AP list. The
tool automatically fetches current AP memberships from New Central
(`GET /monitoring/v2/aps`) and overlays status badges:

| Badge | Colour | Meaning |
|-------|--------|---------|
| **IN SITE** | Amber | AP is already assigned to the target site — auto-deselected |
| **IN GROUP** | Blue | AP is already in its expected `Aruba_<model>` device group |

Deselected APs are not re-submitted to the site assignment API. The device
group step independently checks group membership and skips APs already in
the correct group.

#### Verbose logging

Check **Verbose logging** before clicking Import to log every API call,
HTTP status code, and response body. Useful for diagnosing failures.

#### Import execution

Click **↑ Import to New Central**. Two phases run sequentially:

**Phase 1 — Site assignment**

For each selected group:
1. Collects the selected AP serials
2. Sends them in batches of 50 to `POST /central/v2/sites/associations`

**Phase 2 — Device group assignment**

After all site assignments complete:
1. Fetches the list of existing New Central device groups
   (`GET /configuration/v2/groups`)
2. Fetches each AP's current group via `GET /monitoring/v2/aps`
3. For each AP model present in the import:
   - Creates `Aruba_<model>` group if it does not already exist
   - Moves APs not already in that group via
     `POST /configuration/v1/devices/move`
   - APs already in the correct group are logged as skipped

#### Result cards

A result card is produced for each site assignment and each device group.
Click the summary line on a card to expand the AP list:

- **✓ serial — hostname** (green) — AP moved successfully
- **– serial — hostname** (grey) — AP already assigned, skipped
- **✗ serial — hostname** (red) — move failed

---

### Data tab

**Purpose:** Inspect exported configuration on disk without API calls.

- Sidebar lists all exported groups with AP counts and file indicators
- Click a group to view: group properties, AP inventory table, and
  AOS10 CLI configuration (syntax-highlighted)
- **Filter groups…** box filters by name (case-insensitive substring)
- **⎘ Copy** button copies the raw CLI config to clipboard

#### Sample export

Used for testing the import workflow without a live Classic Central source.

1. Open the **Sample Export** panel at the bottom of the Data tab
2. Toggle it **on**
3. Set the group name and enter serial numbers of real APs that exist in
   your New Central tenant (1–5 APs)
4. Click **Save**

The sample group appears in the Import tab with a yellow **TEST** badge.
The data tab shows the group with a TEST indicator. Disable it before
running a production import.

---

## 8. CLI Scripts Guide

### export_groups.py

```bash
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<token> \
python export_groups.py
```

Exports all groups, writing files to `exports/<group-name>/` and creating
`exports/manifest.json`.

### import_groups.py

```bash
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<target-token> \
python import_groups.py
```

Imports groups from `exports/` to a Classic Central target. Groups already
present on the target are automatically skipped.

### Running inside the container

```bash
docker exec -it central-migration \
  env CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
      CENTRAL_TOKEN=<token> \
  python export_groups.py
```

Files written by the CLI appear immediately in the web UI Data tab.

---

## 9. Export File Format

### Directory structure

```
exports/
├── manifest.json
├── Branch-APs/
│   ├── properties.json
│   ├── ap_cli_config.json
│   ├── country.json
│   ├── ap_inventory.json
│   └── ap_settings/
│       ├── CNABCD1234.json
│       └── CNEFGH5678.json
└── Core-Switches/
    └── properties.json
```

### manifest.json

```json
{
  "_exported_at": "2026-04-28T14:30:00Z",
  "_source_cluster": "https://apigw-prod2.central.arubanetworks.com",
  "groups": ["Branch-APs", "Core-Switches"]
}
```

### ap_inventory.json

Written by the AP inventory exporter. Imported by the New Central import
to map serials to models for device group assignment.

```json
[
  {
    "serial": "CNABCD1234",
    "model": "AP-515",
    "name": "Branch-AP-01",
    "ip_address": "10.1.1.10"
  }
]
```

### properties.json

```json
{
  "allowed_types": ["IAP"],
  "aos10": true,
  "monitor_only_sw": false,
  "monitor_only_cx": false
}
```

### ap_cli_config.json

```json
{
  "cli_config": "version 8.11.2.0\n!\nhostname Branch-APs\n..."
}
```

---

## 10. New Central API Surface

All New Central calls go through `ArubaCentralBase.command()` using the
same token-based auth as Classic Central calls.

| Operation | Method | Endpoint |
|-----------|--------|----------|
| List sites | GET | `/central/v2/sites` |
| Assign APs to site | POST | `/central/v2/sites/associations` |
| List device groups | GET | `/configuration/v2/groups` |
| Create device group | POST | `/configuration/v2/groups` |
| Move APs to device group | POST | `/configuration/v1/devices/move` |
| List APs with membership | GET | `/monitoring/v2/aps` |

### Site assignment payload

```json
{
  "site_id": 43,
  "device_ids": ["CNABCD1234", "CNEFGH5678"],
  "device_type": "IAP"
}
```

### Device group creation payload

```json
{
  "group": "Aruba_AP-515",
  "group_attributes": {
    "template_info": {"Wired": false, "Wireless": true},
    "group_properties": {
      "AllowedDevTypes": ["AccessPoints"],
      "AOSVersion": "AOS10",
      "NewCentral": true
    }
  }
}
```

### Device move payload

```json
{
  "group": "Aruba_AP-515",
  "serials": ["CNABCD1234"]
}
```

### GET /configuration/v2/groups response format

The API returns group names wrapped in single-element lists:

```json
{
  "data": [["aos-cx"], ["Aruba_AP-515"], ["default"]],
  "total": 3
}
```

### GET /monitoring/v2/aps membership fields

```json
{
  "aps": [
    {
      "serial": "CNABCD1234",
      "site": "Branch-Orem",
      "group_name": "Aruba_AP-515"
    }
  ],
  "total": 1
}
```

---

## 11. Flask HTTP API Reference

### POST /api/connect

Test credentials and return all groups.

**Request:**
```json
{"base_url": "https://...", "token": "<access-token>"}
```

**Response:**
```json
{
  "ok": true,
  "groups": ["Branch-APs"],
  "properties": {"Branch-APs": {"allowed_types": ["IAP"], "aos10": true}}
}
```

### POST /api/export — GET /api/export/progress/{id}

Start export. SSE events:

| Event | Payload |
|-------|---------|
| `start` | `{"total": 12}` |
| `group_done` | `{"group": "...", "allowed_types": [...], "files": [...]}` |
| `complete` | `{"total": 12}` |
| `error` | `{"message": "..."}` |

### GET /api/groups

Return summary of all groups on disk including AP counts.

### GET /api/groups/{name}

Return full group detail including AP list with serial, model, hostname, IP.

### POST /api/import/new-central — GET /api/import/new-central/progress/{id}

Start New Central import. Request body:

```json
{
  "base_url": "https://...",
  "token": "<access-token>",
  "verbose": true,
  "mappings": {
    "Branch-APs": {
      "site_id": 43,
      "serials": ["CNABCD1234"]
    }
  }
}
```

SSE events:

| Event | Payload |
|-------|---------|
| `start` | `{"total": 2}` |
| `log` | `{"level": "info|warn|error|debug", "message": "..."}` |
| `group_done` | `{"group": "...", "site_id": 43, "status": "ok|fail|missing", "ap_count": 1, "serials": [...], "failed_serials": [...], "steps": [...]}` |
| `complete` | `{"total": 2, "dg_results": [{"model": "AP-515", "group_name": "Aruba_AP-515", "total": 1, "ok": true, "serials": [...], "skipped_serials": [...], "failed_serials": [...]}]}` |
| `error` | `{"message": "..."}` |

### POST /api/import/new-central/sites

Fetch all sites from a New Central instance.

**Request:** `{"base_url": "...", "token": "..."}`

**Response:** `{"ok": true, "sites": [{"site_id": 43, "site_name": "Branch-Orem"}]}`

### POST /api/import/new-central/memberships

Return current site and device-group membership for a list of AP serials.

**Request:** `{"base_url": "...", "token": "...", "serials": ["CNABCD1234"]}`

**Response:**
```json
{
  "ok": true,
  "memberships": {
    "CNABCD1234": {
      "site_name": "Branch-Orem",
      "device_group": "Aruba_AP-515"
    }
  }
}
```

### GET /api/sample — POST /api/sample

Read or write the sample export configuration used for testing.

### GET /health

Returns `{"ok": true}` with HTTP 200 when the service is ready.

---

## 12. Troubleshooting

### Docker: container exits immediately

```bash
docker compose logs central-migration
```

**Port already in use:**
```
[ERROR] Connection in use: ('0.0.0.0', 8000)
```
Change the host port in `docker-compose.yml`.

**Permissions on exports/:**
```
PermissionError: [Errno 13] Permission denied: '/app/exports/manifest.json'
```
The container runs as uid 1001. Fix host directory ownership:
```bash
sudo chown -R 1001:1001 ./exports
```

---

### Connect fails: timeout or connection refused

The Flask backend makes API calls server-side. The Docker host needs
network access to the Central API Gateway — not just the browser machine.
Test from inside the container:

```bash
docker exec central-migration python -c "
import urllib.request
r = urllib.request.urlopen('https://apigw-prod2.central.arubanetworks.com')
print(r.status)
"
```

---

### Connect fails: HTTP 401

Token expired (tokens are valid for 2 hours) or incorrect token. Generate
a fresh token from the Central API Gateway page.

---

### Connect fails: HTTP 403

Valid token but user account lacks API access or required role. The user
must have **Admin** access for imports; **Read-Only** is sufficient for
export and data browsing.

---

### Import: APs not appearing in device group

Enable **Verbose logging** and re-run the import. The log will show:

1. The list of existing device groups fetched from New Central
2. Each AP's current device group from the monitoring API
3. The create-group API response (if creation was attempted)
4. The move API response for each batch

Common causes:
- AP serial not found in `ap_inventory.json` — re-export from Classic
  Central with the latest exporter version
- AP not onboarded in New Central — the AP must be visible in the New
  Central device inventory before it can be moved between groups

---

### SSE progress stream stops mid-operation

A proxy or load balancer is buffering the SSE stream. Check:

1. nginx: confirm `proxy_buffering off` and `proxy_read_timeout 180s`
2. Cloud load balancers: idle timeout must exceed the gunicorn `--timeout`

Increase the gunicorn timeout via environment variable:
```yaml
# docker-compose.yml
environment:
  - GUNICORN_CMD_ARGS=--bind=0.0.0.0:8000 --workers=1 --threads=8 --timeout=300
```

---

### Data tab shows "No export on disk"

No `manifest.json` in `exports/`. Run an export first, or copy an existing
export directory into `./exports/` on the host.

---

## 13. Extending the Tool

### Adding a new data type to export

All export/import logic is registered in `EXPORTERS` in `exporters.py`.

```python
# exporters.py

def export_wlan_profiles(central, group_name, group_dir, **_):
    resp = central.command(
        apiMethod="GET",
        apiPath=f"/configuration/v1/wlan/{group_name}"
    )
    if resp["code"] != 200:
        return
    _save(group_dir, "wlan_profiles.json", resp["msg"])

def import_wlan_profiles(central, group_name, group_dir):
    data = _load(group_dir, "wlan_profiles.json")
    if data is None:
        return True
    resp = central.command(
        apiMethod="PUT",
        apiPath=f"/configuration/v1/wlan/{group_name}",
        apiData=data
    )
    return resp["code"] in (200, 201)

EXPORTERS = [
    ...
    {
        "name":       "wlan_profiles",
        "applies_to": {"IAP"},
        "export_fn":  export_wlan_profiles,
        "import_fn":  import_wlan_profiles,
    },
]
```

No changes to `app.py`, `export_groups.py`, or `import_groups.py` are
needed. The new entry is automatically picked up by `get_active_exporters()`
and will run for all groups whose `allowed_types` intersects `applies_to`.

### Rebuilding after code changes

```bash
docker compose up -d --build
```

The pip install layer is cached — only changed Python files are re-copied.
A typical code-only rebuild takes under 10 seconds.
