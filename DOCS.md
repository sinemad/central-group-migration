# HPE Aruba Central — Group Migration Tool
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
10. [pycentral SDK Reference](#10-pycentral-sdk-reference)
11. [API Reference](#11-api-reference)
12. [Troubleshooting](#12-troubleshooting)
13. [Extending the Tool](#13-extending-the-tool)

---

## 1. Overview

This tool exports group configurations from one HPE Aruba Classic Central
instance and re-imports them to another. It is designed specifically for
**AOS10 UI groups** — groups where device configuration is managed via the
Central web interface rather than CLI templates.

**What is exported per group:**

| File | Contents | Condition |
|------|----------|-----------|
| `properties.json` | Allowed device types, AOS10 flag, monitor-only flags | All groups |
| `ap_cli_config.json` | Full AOS10 CLI configuration blob | IAP groups only |
| `country.json` | RF country code | IAP groups only |

**What is not exported:**

- Template groups (not supported — tool is AOS10 UI groups only)
- Device assignments (devices must be re-assigned to groups separately)
- Labels and sites (separate API category, not in scope)
- Firmware compliance policies

---

## 2. Architecture

```
central-migration/
├── app.py                 # Flask backend — all routes, SSE, orchestration
├── exporters.py           # Per-data-type export/import logic
│                          #   shared by both the web UI and CLI scripts
├── export_groups.py       # Standalone CLI export script
├── import_groups.py       # Standalone CLI import script
├── templates/
│   └── index.html         # Single-file web UI (Export / Import / Data tabs)
├── exports/               # Runtime data directory — bind-mounted as Docker volume
│   ├── manifest.json
│   └── <group-name>/
│       ├── properties.json
│       ├── ap_cli_config.json
│       └── country.json
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .dockerignore
```

**Request flow (web UI):**

```
Browser
  │
  │  HTTP + SSE
  ▼
Flask (gunicorn, 1 worker / 8 threads)
  │  app.py routes
  │
  ├── /api/connect        ──► pycentral.ArubaCentralBase.command()
  ├── /api/export         ──► background thread ──► exporters.py ──► pycentral
  ├── /api/export/progress/<id>  ──► SSE stream from queue
  ├── /api/import         ──► background thread ──► exporters.py ──► pycentral
  ├── /api/import/progress/<id>  ──► SSE stream from queue
  ├── /api/groups         ──► reads exports/ directory from disk
  ├── /api/groups/<name>  ──► reads exports/<name>/ from disk
  └── /health             ──► {"ok": true}
```

**Why single gunicorn worker:**
Export and import operations push progress events into `_progress_queues`,
an in-process Python dict. SSE subscriber threads read from this same dict.
Multiple gunicorn workers each have isolated memory — an export started in
worker A would be invisible to a subscriber connected to worker B, causing
the progress stream to hang. One worker with eight threads is the correct
model for this pattern.

---

## 3. Dependencies and Package Verification

### requirements.txt

```
pycentral==1.4.1
flask==3.1.0
flask-cors==5.0.0
gunicorn==23.0.0
```

### Docker build verification

The Docker `builder` stage runs:

```dockerfile
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
```

This was verified by simulating the build stage locally — installing all
four packages into an isolated prefix and importing them:

```
pycentral        : 1.4.1   ✓  installed from PyPI
flask            : 3.1.0   ✓  installed from PyPI
flask-cors       : 5.0.0   ✓  installed from PyPI
gunicorn         : 23.0.0  ✓  installed from PyPI
```

Key classes confirmed importable from the installed prefix:

```python
from pycentral.base import ArubaCentralBase   # ✓
from pycentral.configuration import Groups    # ✓
```

pycentral 1.4.1 pulls in its own pinned dependencies automatically:

```
requests==2.31.0
PyYAML==6.0.1
urllib3==2.2.2
certifi==2024.7.4
```

These are installed alongside pycentral in the same build stage and copied
into the runtime image. You do not need to list them in `requirements.txt`.

### Verifying inside a running container

```bash
docker exec central-migration python -c "
import pycentral
from pycentral.base import ArubaCentralBase
from pycentral.configuration import Groups
import flask, gunicorn
import importlib.metadata
for pkg in ['pycentral', 'flask', 'flask-cors', 'gunicorn']:
    print(pkg, importlib.metadata.version(pkg))
"
```

Expected output:

```
pycentral 1.4.1
flask 3.1.0
flask-cors 5.0.0
gunicorn 23.0.0
```

---

## 4. Docker Deployment

### Prerequisites

- Docker Engine 24.0 or later
- Docker Compose v2 (`docker compose`, not `docker-compose`)
- Network access to the HPE Aruba Central API Gateway from the Docker host

### Build and start

```bash
# Clone or copy the project directory, then:
docker compose up -d
```

`docker compose up` triggers a full build on first run. During the build,
pip downloads and installs pycentral and all other dependencies from PyPI.
You can watch this happen:

```bash
docker compose build --progress=plain
```

You will see output similar to:

```
#7 [builder 3/3] RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
#7 0.812 Collecting pycentral==1.4.1
#7 1.043   Downloading pycentral-1.4.1-py3-none-any.whl (73 kB)
#7 1.218 Collecting flask==3.1.0
#7 1.389   Downloading flask-3.1.0-py3-none-any.whl (102 kB)
#7 1.502 Collecting flask-cors==5.0.0
#7 1.598   Downloading flask_cors-5.0.0-py2.py3-none-any.whl (14 kB)
#7 1.701 Collecting gunicorn==23.0.0
#7 1.798   Downloading gunicorn-23.0.0-py3-none-any.whl (85 kB)
...
#7 DONE
```

### Open the UI

```
http://localhost:8000
```

### Common Docker Compose commands

```bash
# Start in background
docker compose up -d

# View live logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build

# Stop and remove container (exports/ data is preserved on host)
docker compose down

# Stop, remove container and volume
docker compose down -v

# Check health status
docker inspect central-migration --format='{{.State.Health.Status}}'
```

### Exported data location

The `exports/` directory is bind-mounted from the host:

```yaml
volumes:
  - ./exports:/app/exports
```

This means all exported group data is written to `./exports/` relative to
the `docker-compose.yml` file on the host machine. The data persists across
container restarts, image rebuilds, and `docker compose down`.

### Changing the port

Edit `docker-compose.yml`:

```yaml
ports:
  - "9090:8000"   # expose on host port 9090 instead of 8000
```

The container always listens on `8000` internally. Only the host-side port
changes.

### Running behind a reverse proxy (nginx example)

```nginx
server {
    listen 80;
    server_name central-migration.internal;

    location / {
        proxy_pass         http://localhost:8000;
        proxy_http_version 1.1;

        # Required for SSE (Server-Sent Events)
        proxy_set_header   Connection '';
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 180s;
    }
}
```

The `proxy_buffering off` and `proxy_read_timeout 180s` settings are
critical — without them, SSE progress streams will be buffered or cut off
mid-export.

---

## 5. Local Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python app.py
# Starts Flask dev server at http://localhost:5000
```

The dev server uses Flask's built-in reloader — save any `.py` file and
the server restarts automatically. Do not use the dev server in production.

### Running the CLI scripts locally

```bash
# Export
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<your-token> \
python export_groups.py

# Import
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<new-tenant-token> \
python import_groups.py
```

Alternatively, edit `central_info` directly in each script.

---

## 6. Authentication

### Access token (recommended)

The tool uses the access token authentication path of `ArubaCentralBase`.
This is the most secure approach — credentials are not stored or cached.

```python
central_info = {
    "base_url": "https://apigw-prod2.central.arubanetworks.com",
    "token": {
        "access_token": "<api-gateway-access-token>"
    }
}
conn = ArubaCentralBase(central_info=central_info, token_store=None, ssl_verify=True)
```

### Finding your base URL

Base URLs are cluster-specific. Common examples:

| Cluster | Base URL |
|---------|----------|
| US-1 | `https://apigw-prod2.central.arubanetworks.com` |
| US-2 | `https://apigw-prod2-eu.central.arubanetworks.com` |
| EU-1 | `https://eu-apigw.central.arubanetworks.com` |
| APAC-1 | `https://apigw-apac.central.arubanetworks.com` |

Confirm your cluster URL from Central UI: **Maintain → Organization →
Platform Integration → API Gateway**.

### Generating an access token

1. In Central UI, navigate to **Maintain → Organization → Platform Integration → API Gateway**
2. Click **My Apps & Tokens**
3. Select your application, then click **Generate Token**
4. Copy the `access_token` value — it is valid for 2 hours

Tokens expire after 2 hours. For long-running operations, use the OAuth
credentials approach which enables automatic token refresh:

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

The customer ID is visible in the top-right corner of the Central UI (the
person icon → account info).

---

## 7. Web UI Guide

Open `http://localhost:8000` (or your configured host/port). The interface
has three tabs.

### Export tab

**Purpose:** Connect to a source Classic Central instance, select groups,
and write their configuration to disk.

**Step-by-step:**

1. Enter the **Base URL** of the source cluster
2. Enter an **Access Token** for the source cluster
3. Click **Connect & Load Groups** — the sidebar populates with all groups
   found, each labelled with its device type (`IAP`, `SW`, `CX`, `MIXED`)
4. Use the checkboxes to select which groups to export. **All** and **None**
   buttons select or clear all groups
5. Click **↓ Export Selected**

The main panel shows real-time progress as each group is exported:

- A progress bar tracking groups completed
- Summary counters: Exported / Warnings / Failed
- A result card per group showing which files were written
- A timestamped log terminal at the bottom

**Result card status meanings:**

| Status | Meaning |
|--------|---------|
| `OK` | All files written successfully |
| `WARN` | Group created but one file (e.g. country code) returned a non-200 response |
| `FAIL` | Group properties could not be fetched |

### Import tab

**Purpose:** Load an export from disk and re-create the groups on a target
Classic Central instance.

**Step-by-step:**

1. Click **⬡ Load Export from Disk** — the sidebar populates from
   `manifest.json` on disk. The manifest metadata (source cluster, export
   timestamp, group count) is shown in the main panel
2. Use checkboxes to select which groups to import
3. Enter the **Base URL** and **Access Token** for the **target** cluster
4. Click **↑ Import Selected**

Groups already present on the target instance are automatically detected
and skipped — they appear in the results as `SKIPPED` with a grey indicator.
The import is safe to re-run.

Per-group result cards show each import step:

- **Create group** — POST to `/configuration/v3/groups`
- **CLI config** — PUT to `/configuration/v1/ap_config/{group}` (IAP groups only)
- **Country code** — PUT to `/configuration/v1/country` (IAP groups only)

Each step has a green or red dot. A group is marked `FAIL` only if one or
more steps return a non-2xx response.

### Data tab

**Purpose:** Browse and inspect exported configuration currently on disk,
without making any API calls to Central.

**Using the Data tab:**

1. Click the **Data** tab — the browser automatically loads from disk
2. The main panel shows:
   - Manifest metadata (source cluster, export timestamp)
   - Summary cards: total groups, IAP groups, switch groups, groups with CLI config
3. Click any group in the sidebar to load its detail view

**Group detail view shows:**

- **File pills** (top right of card) — green when the file is present on
  disk, grey when absent
- **Properties grid** — `allowed_types`, `aos10`, `monitor_only_sw`,
  `monitor_only_cx`, `country_code`
- **CLI config block** (IAP groups only) — the full AOS10 configuration
  rendered with syntax highlighting:
  - Blue — CLI keywords (`wlan`, `ssid`, `security`, `interface`, etc.)
  - Amber — indented configuration keys
  - Purple — numeric values and port numbers
  - Cyan — quoted string values
  - Green — IP addresses
  - Grey italic — comment lines beginning with `!` or `#`
- **⎘ Copy** button — copies the raw CLI config to the clipboard

Use the **Filter groups…** search box in the sidebar to filter by group
name. The filter is case-insensitive and matches substrings.

---

## 8. CLI Scripts Guide

The CLI scripts (`export_groups.py`, `import_groups.py`) use the same
`exporters.py` module as the web UI and write to the same `exports/`
directory. They can be used interchangeably with the web UI — run an export
via CLI and import via web UI, or vice versa.

### export_groups.py

Exports all groups from a Classic Central instance.

```bash
# Using environment variables (recommended)
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<token> \
python export_groups.py
```

Example output:

```
Found 12 groups

Branch-APs/  (types: ['IAP'])
    properties.json
    ap_cli_config.json
    country.json  (US)
Core-Switches/  (types: ['ArubaSwitch', 'CX'])
    properties.json
HQ-APs/  (types: ['IAP'])
    properties.json
    ap_cli_config.json
    country.json  (US)
...

Saved manifest (12 groups)
Export complete
```

### import_groups.py

Imports groups from the `exports/` directory to a target Classic Central
instance. Groups already present on the target are automatically skipped.

```bash
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<new-tenant-token> \
python import_groups.py
```

Example output:

```
Manifest loaded: 12 groups
Source cluster:  https://apigw-prod2.central.arubanetworks.com
Exported at:     2026-04-06T14:30:00Z

Skipping 2 group(s) already present on target

Branch-APs/  (types: ['IAP'])
  [Branch-APs] Group created
  [Branch-APs] CLI config pushed
  [Branch-APs] Country set: US
Core-Switches/  (types: ['ArubaSwitch', 'CX'])
  [Core-Switches] Group created
...

Import complete:
  Created: 10
  Failed:  0
  Missing: 0
```

### Running CLI scripts inside the container

```bash
docker exec -it central-migration \
  env CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
      CENTRAL_TOKEN=<token> \
  python export_groups.py
```

The `exports/` directory inside the container is the same bind-mounted
path, so files written by the CLI script are immediately visible in the
web UI's Data tab.

---

## 9. Export File Format

### Directory structure

```
exports/
├── manifest.json
├── Branch-APs/
│   ├── properties.json
│   ├── ap_cli_config.json
│   └── country.json
├── Core-Switches/
│   └── properties.json
└── HQ-APs/
    ├── properties.json
    ├── ap_cli_config.json
    └── country.json
```

### manifest.json

Written last, after all groups are exported. Preserves the group order
returned by the Central API, which the import respects.

```json
{
  "_exported_at": "2026-04-06T14:30:00Z",
  "_source_cluster": "https://apigw-prod2.central.arubanetworks.com",
  "groups": ["Branch-APs", "Core-Switches", "HQ-APs"]
}
```

### properties.json

Returned directly from `GET /configuration/v1/groups/properties`. The
payload is stored as-is and re-submitted to `POST /configuration/v3/groups`
on import.

```json
{
  "allowed_types": ["IAP"],
  "aos10": true,
  "monitor_only_sw": false,
  "monitor_only_cx": false
}
```

`allowed_types` controls which device tabs are visible in the Central UI
for that group. Valid values: `IAP`, `ArubaSwitch`, `CX`, `MobilityController`.

### ap_cli_config.json

The CLI config blob returned from `GET /configuration/v1/ap_config/{group}`.
Wrapped in a single key so the file is valid JSON regardless of the CLI
content format.

```json
{
  "cli_config": "version 8.11.2.0\n!\nhostname Branch-APs\n!\nwlan ssid-profile Corp-WiFi\n  ..."
}
```

On import, `cli_config` is extracted and PUT as-is to
`/configuration/v1/ap_config/{group}`. This is a full replace — the entire
group configuration is overwritten.

### country.json

```json
{
  "country": "US"
}
```

Country codes follow ISO 3166-1 alpha-2 format. On import, applied via
`PUT /configuration/v1/country` with `{"groups": ["<name>"], "country": "US"}`.

---

## 10. pycentral SDK Reference

All Central API calls in this project use `ArubaCentralBase` and the
`Groups` class from `pycentral.configuration`.

### ArubaCentralBase

```python
from pycentral.base import ArubaCentralBase

conn = ArubaCentralBase(
    central_info=central_info,  # dict with base_url + token or OAuth creds
    token_store=None,           # None = no local token caching
    ssl_verify=True,            # set False only for lab environments with self-signed certs
    user_retries=10             # retry attempts on transient failures
)
```

### conn.command()

The primary method for all API calls. Returns a dict with two keys:

```python
resp = conn.command(
    apiMethod="GET",                        # HTTP method: GET, POST, PUT, DELETE, PATCH
    apiPath="/configuration/v2/groups",     # API path (with or without leading slash)
    apiParams={"limit": 20, "offset": 0},  # URL query parameters
    apiData={"group": "MyGroup", ...}       # Request body (serialised to JSON automatically)
)

# Response structure:
# resp["code"]  — HTTP status code (int)
# resp["msg"]   — Parsed JSON response body (dict or list) or raw string
```

### Groups class

```python
from pycentral.configuration import Groups

g = Groups()
resp = g.get_groups(conn, offset=0, limit=20)
# Wraps GET /configuration/v2/groups
# resp["msg"]["data"] — list of group name strings
```

The API returns a maximum of 20 groups per call. Use offset pagination to
retrieve all groups:

```python
all_groups, offset, limit = [], 0, 20
while True:
    resp = g.get_groups(conn, offset=offset, limit=limit)
    page = resp["msg"].get("data", [])
    all_groups.extend(page)
    if len(page) < limit:
        break
    offset += limit
```

### URL constants used

These are the Central API endpoints called by this tool:

| Operation | Method | Endpoint |
|-----------|--------|----------|
| List groups | GET | `/configuration/v2/groups` |
| Group properties | GET | `/configuration/v1/groups/properties` |
| AP CLI config | GET | `/configuration/v1/ap_config/{group}` |
| Country code | GET | `/configuration/v1/{group}/country` |
| Create group | POST | `/configuration/v3/groups` |
| Push CLI config | PUT | `/configuration/v1/ap_config/{group}` |
| Set country code | PUT | `/configuration/v1/country` |

---

## 11. API Reference

The Flask backend exposes the following HTTP API, consumed by the web UI.

### POST /api/connect

Test credentials and return all groups with their properties.

**Request:**
```json
{
  "base_url": "https://apigw-prod2.central.arubanetworks.com",
  "token": "<access-token>"
}
```

**Response:**
```json
{
  "ok": true,
  "groups": ["Branch-APs", "Core-Switches"],
  "properties": {
    "Branch-APs": {"allowed_types": ["IAP"], "aos10": true, ...},
    "Core-Switches": {"allowed_types": ["ArubaSwitch", "CX"], ...}
  }
}
```

### POST /api/export

Start an export operation. Returns an `op_id` immediately; progress is
streamed via SSE.

**Request:**
```json
{
  "base_url": "https://apigw-prod2.central.arubanetworks.com",
  "token": "<access-token>",
  "groups": ["Branch-APs", "HQ-APs"]   // empty array = export all
}
```

**Response:** `{"ok": true, "op_id": "export_1712412600000"}`

### GET /api/export/progress/{op_id}

Server-Sent Events stream. Connect with `EventSource` to receive progress.

**Events:**

| Event | Payload |
|-------|---------|
| `start` | `{"total": 12}` |
| `group_done` | `{"group": "Branch-APs", "allowed_types": ["IAP"], "files": [...]}` |
| `complete` | `{"total": 12}` |
| `error` | `{"message": "..."}` |

### GET /api/manifest

Read the manifest from disk.

**Response:**
```json
{
  "ok": true,
  "manifest": {"_exported_at": "...", "_source_cluster": "...", "groups": [...]},
  "groups": [
    {"name": "Branch-APs", "allowed_types": ["IAP"], "files": ["properties.json", ...]}
  ]
}
```

### POST /api/import

Start an import operation. Returns `op_id`; progress via SSE.

**Request:**
```json
{
  "base_url": "https://apigw-prod2.central.arubanetworks.com",
  "token": "<target-access-token>",
  "groups": ["Branch-APs"]   // empty array = import all from manifest
}
```

**Response:** `{"ok": true, "op_id": "import_1712412900000"}`

### GET /api/import/progress/{op_id}

**Events:**

| Event | Payload |
|-------|---------|
| `start` | `{"total": 10, "skipped": ["existing-group"]}` |
| `group_done` | `{"group": "Branch-APs", "status": "ok", "steps": [{"name": "Create group", "ok": true}, ...]}` |
| `complete` | `{"total": 10, "skipped": 2}` |
| `error` | `{"message": "..."}` |

### GET /api/groups

Return summary of all groups on disk.

**Response:**
```json
{
  "ok": true,
  "manifest": {"_exported_at": "...", ...},
  "groups": [
    {
      "name": "Branch-APs",
      "allowed_types": ["IAP"],
      "aos10": true,
      "monitor_only_sw": false,
      "monitor_only_cx": false,
      "country": "US",
      "has_cli_config": true,
      "files": ["properties.json", "ap_cli_config.json", "country.json"]
    }
  ]
}
```

### GET /api/groups/{group_name}

Return full detail for a single group including CLI config.

**Response:**
```json
{
  "ok": true,
  "name": "Branch-APs",
  "properties": {"allowed_types": ["IAP"], "aos10": true, ...},
  "country": "US",
  "cli_config": "version 8.11.2.0\n!\nhostname Branch-APs\n..."
}
```

### GET /health

Health check. Returns HTTP 200 when the service is ready.

**Response:** `{"ok": true}`

---

## 12. Troubleshooting

### Docker: container exits immediately

```bash
docker compose logs central-migration
```

Common causes:

**Port already in use:**
```
[ERROR] Connection in use: ('0.0.0.0', 8000)
```
Change the host port in `docker-compose.yml`:
```yaml
ports:
  - "8001:8000"
```

**Permissions on exports/ directory:**
```
PermissionError: [Errno 13] Permission denied: '/app/exports/manifest.json'
```
The container runs as uid 1001. If the host `exports/` directory was created
by root, fix ownership:
```bash
sudo chown -R 1001:1001 ./exports
```

---

### Connect fails: "Connection refused" or timeout

The Flask backend makes the API call server-side. The Docker host must have
network connectivity to the Central API Gateway, not the browser machine.

```bash
# Test from inside the container
docker exec central-migration python -c "
import urllib.request
r = urllib.request.urlopen('https://apigw-prod2.central.arubanetworks.com')
print(r.status)
"
```

If this fails, check firewall rules on the Docker host — port 443 outbound
to Central's API Gateway must be open.

---

### Connect fails: HTTP 401

Token has expired (tokens are valid for 2 hours) or the wrong token was
entered. Generate a fresh token from the Central API Gateway page and retry.

---

### Connect fails: HTTP 403

The token is valid but the associated user account does not have API access
or lacks the required role. In Central, the user must have at minimum
**Read-Only** access. For imports, **Admin** access is required.

---

### Export: group exported with WARN status

A `WARN` on a file means that file's API call returned a non-200 status but
did not block the rest of the export. Common causes:

- **country.json WARN** — the group has no country code set. This is
  expected for groups where APs bring their own country code. The file is
  not written, and import skips the country code step for that group.
- **ap_cli_config.json WARN** — the group exists but has no AP configuration
  yet (empty group with no devices). The file is not written.

---

### Import: group fails at "Create group" step

Check the import log for the HTTP response code:

- **409 Conflict** — the group name already exists on the target. This
  should not happen if the idempotency check is working. Manually delete
  the group in Central and retry.
- **400 Bad Request** — the `properties.json` payload contains a field the
  target instance does not support (e.g. a newer API feature). Inspect the
  file and remove unrecognised fields.
- **422 Unprocessable Entity** — the group name contains characters not
  permitted by Central (max 32 single-byte ASCII characters, no spaces or
  special characters).

---

### Import: "CLI config pushed" but configuration is wrong in Central

`PUT /configuration/v1/ap_config` is a full replace. If the target group
already had configuration before the import, it was overwritten. This is
expected behaviour — ensure the target group is empty before running an
import that includes AP CLI config.

---

### SSE progress stream stops mid-export

This happens when a proxy or load balancer buffers or drops the SSE
connection. Check:

1. If using nginx, confirm `proxy_buffering off` and `proxy_read_timeout 180s`
2. If using a cloud load balancer, check its idle timeout setting — it must
   be greater than the gunicorn `--timeout` value (120s)

You can also increase the gunicorn timeout by setting `GUNICORN_CMD_ARGS`
in `docker-compose.yml`:

```yaml
environment:
  - GUNICORN_CMD_ARGS=--bind=0.0.0.0:8000 --workers=1 --threads=8 --timeout=300 --keep-alive=5 --access-logfile=- --error-logfile=-
```

---

### Data tab shows "No export on disk"

No `manifest.json` exists in `exports/`. Run an export first. If you ran
a CLI export, confirm it ran in the same directory that Docker maps to
`/app/exports` inside the container.

```bash
ls -la ./exports/
# Should show manifest.json and one directory per group
```

---

### pycentral raises "urllib3 RequestsDependencyWarning"

```
urllib3 (2.x.x) or chardet/charset_normalizer doesn't match a supported version
```

This is an advisory warning, not an error. pycentral 1.4.1 pins
`urllib3==2.2.2` and `requests==2.31.0`. If your system has a newer urllib3
installed alongside, the warning appears. It does not affect API call
behaviour. In the Docker image this warning does not appear because the
clean build installs exactly the pinned versions.

---

## 13. Extending the Tool

### Adding a new data type to export

All export/import logic is registered in `EXPORTERS` in `exporters.py`.
Adding a new data type requires three things:

1. An `export_fn` that writes a file to `group_dir`
2. An `import_fn` that reads that file and calls the appropriate Central API
3. A registry entry declaring which device types trigger the exporter

**Example — exporting switch ACLs for CX groups:**

```python
# In exporters.py

def export_switch_acls(central, group_name: str, group_dir: str, **_):
    resp = central.command(
        apiMethod="GET",
        apiPath=f"/configuration/v1/cx_devices/acl/{group_name}"
    )
    if resp["code"] != 200:
        print(f"    switch_acls.json  [WARN: {resp['code']} — skipped]")
        return
    _save(group_dir, "switch_acls.json", resp["msg"])
    print(f"    switch_acls.json")


def import_switch_acls(central, group_name: str, group_dir: str) -> bool:
    data = _load(group_dir, "switch_acls.json")
    if data is None:
        return True
    resp = central.command(
        apiMethod="PUT",
        apiPath=f"/configuration/v1/cx_devices/acl/{group_name}",
        apiData=data
    )
    return resp["code"] in (200, 201)


# Add to EXPORTERS:
EXPORTERS = [
    ...
    {
        "name": "switch_acls",
        "applies_to": {"CX"},       # only runs for groups with CX switches
        "export_fn": export_switch_acls,
        "import_fn": import_switch_acls,
    },
]
```

No changes to `app.py`, `export_groups.py`, or `import_groups.py` are
needed. The new exporter is automatically picked up by `get_active_exporters()`
and will:

- Run during web UI export for groups with `CX` in `allowed_types`
- Write `switch_acls.json` to the group directory
- Appear as a file pill in the Data tab (if the file is present on disk)
- Run during web UI and CLI import for applicable groups

### Rebuilding the Docker image after code changes

```bash
docker compose up -d --build
```

The builder stage caches the pip install layer. If `requirements.txt` has
not changed, pip does not re-download packages — only changed Python files
are re-copied. A typical rebuild after a code-only change takes under 10
seconds.
