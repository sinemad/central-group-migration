# HPE Aruba Central Group Migration Tool

Migrate access point configuration from **Classic Central** (UI groups) to
**New Central** (sites and device groups). Provides a web UI, CLI scripts,
and a Docker container.

> **Scope:** AOS10 UI groups in Classic Central. Template groups and
> pre-AOS10 (Instant) groups are not supported.

---

## What this tool does

Classic Central organises APs into *groups*. New Central uses *sites* (for
location) and *device groups* (for configuration profile). This tool:

1. **Exports** group configuration from a Classic Central tenant to disk
2. **Imports** the APs into the equivalent New Central site and device group,
   using a visual mapping workflow

---

## Quick start (Docker — recommended)

```bash
git clone https://github.com/<your-org>/central-group-migration.git
cd central-group-migration
docker compose up -d
open http://localhost:8000
```

Docker downloads and installs all dependencies on first build. No other
setup is required.

---

## Local development setup

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                   # Dev server at http://localhost:5000
```

---

## Authentication — generating an access token

Both the source (Classic Central) and target (New Central) require an
access token.

1. In the Central UI go to **Maintain → Organization → Platform Integration
   → API Gateway → My Apps & Tokens**
2. Select your application and click **Generate Token**
3. Copy the `access_token` value

Tokens are valid for **2 hours**. Generate a fresh token before starting a
large export or import. See [DOCS.md § Authentication](DOCS.md#6-authentication)
for the OAuth credential approach that refreshes automatically.

**Cluster base URLs (common):**

| Region | Base URL |
|--------|----------|
| US-1 | `https://apigw-prod2.central.arubanetworks.com` |
| US-2 | `https://apigw-prod2-eu.central.arubanetworks.com` |
| EU-1 | `https://eu-apigw.central.arubanetworks.com` |
| APAC-1 | `https://apigw-apac.central.arubanetworks.com` |

Your base URL is shown in **Maintain → Organization → Platform Integration
→ API Gateway**.

---

## Web UI

Open `http://localhost:8000` (Docker) or `http://localhost:5000` (local
dev). Three tabs:

### Export tab

Connects to a Classic Central source tenant and writes group configuration
to disk.

1. Enter the **Base URL** and **Access Token** for the source Classic
   Central instance
2. Click **Connect & Load Groups** — the sidebar populates with all groups
3. Select the groups to export using the checkboxes (**All** / **None**
   buttons are available)
4. Click **↓ Export Selected**

Real-time progress streams as each group is exported. Result cards show
which files were written per group (`properties.json`, `ap_cli_config.json`,
`ap_inventory.json`, etc.).

---

### Import tab

Assigns APs from the exported Classic Central groups to existing sites and
device groups in New Central.

#### Step 1 — Load the export

Click **⬡ Load Export from Disk**. The sidebar populates from `manifest.json`
on disk. Exported groups default to **no selection** — check the groups you
want to import.

#### Step 2 — Connect to New Central

Enter the **Base URL** and **Access Token** for the **New Central** target
instance and click **Connect to New Central**. The tool fetches all existing
sites and auto-matches them to exported groups by name.

#### Step 3 — Map groups to sites

The **Sites** mapping panel shows every selected group with a site dropdown.
Groups whose name exactly matches a New Central site are auto-matched
(shown with a green ✓ auto badge). Unmatched groups can be manually assigned
from the dropdown.

Groups with no site mapping are skipped during import.

#### Step 4 — Review and select APs

Click the **▶** expand button next to any group to see the APs in that
group's export. Each AP row shows:

- Serial number and hostname
- Model
- IP address
- **IN SITE** badge (amber) — the AP is already assigned to the target site
  and will be automatically deselected
- **IN GROUP** badge (blue) — the AP is already in its expected New Central
  device group (`Aruba_<model>`)

Use the **All** / **None** buttons or individual checkboxes to control which
APs are imported. Membership data is fetched automatically from New Central
once you have connected.

#### Step 5 — Run the import

Enable **Verbose logging** (checkbox) to see every API call and response in
the log. Click **↑ Import to New Central**.

The import performs two operations for each selected group:

**Site assignment** — assigns the selected APs to the mapped New Central
site via `POST /central/v2/sites/associations`.

**Device group assignment** — after all site assignments complete, APs are
moved to model-based device groups (e.g. `Aruba_AP-515`). Groups are
created automatically if they do not exist. APs already in the correct
device group are skipped.

#### Import result cards

A result card is shown for each site assignment and each device group
assignment. Click the summary line on any card to expand the AP list:

- **✓ serial — hostname** in green — AP was successfully moved
- **– serial — hostname** in grey — AP was already assigned (skipped)
- **✗ serial — hostname** in red — AP move failed

---

### Data tab

Browse exported configuration on disk without making any API calls.

- Sidebar lists all exported groups with AP counts
- Click a group to view its detail: file inventory, group properties, AP
  list, and AOS10 CLI configuration (syntax-highlighted)
- **Filter groups…** search box filters by name (case-insensitive substring)

#### Sample export (testing)

The Data tab includes a **Sample Export** panel for testing the import
workflow without a live Classic Central source. Toggle it on, set a group
name, and enter the serial numbers of real APs that exist in your New
Central tenant. The sample group appears in the Import tab with a yellow
**TEST** badge.

> Disable the sample export before running a production import.

---

## CLI scripts

The CLI scripts share the same logic as the web UI and write to the same
`exports/` directory.

```bash
# Export all groups from Classic Central
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<token> \
python export_groups.py

# Import groups to a target Classic Central instance
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<target-token> \
python import_groups.py
```

---

## Docker reference

```bash
# Start in background (builds on first run)
docker compose up -d

# Rebuild after code changes
docker compose up -d --build

# View live logs
docker compose logs -f

# Stop (exported data in ./exports/ is preserved)
docker compose down
```

The `exports/` directory is bind-mounted from the host — data persists
across restarts and rebuilds.

To change the host port, edit `docker-compose.yml`:
```yaml
ports:
  - "9090:8000"   # change 9090 to any available host port
```

---

## Project layout

```
central-group-migration/
├── app.py                   # Flask backend (routes, SSE, orchestration)
├── exporters.py             # Per-data-type export/import registry
├── new_central_importer.py  # New Central site and device group import logic
├── export_groups.py         # CLI export script
├── import_groups.py         # CLI import script
├── templates/
│   └── index.html           # Web UI — Export / Import / Data tabs
├── exports/                 # Runtime data (bind-mounted, git-ignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md                # This file
└── DOCS.md                  # Full technical documentation
```

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pycentral` | ≥ 2.0a17 | Aruba Central Python SDK — all API calls |
| `flask` | 3.1.0 | Web framework |
| `flask-cors` | 5.0.0 | CORS headers |
| `gunicorn` | 23.0.0 | Production WSGI server (single worker, 8 threads) |

---

## Full documentation

See **[DOCS.md](DOCS.md)** for:

- Architecture and request flow
- Docker build verification
- Authentication (access tokens and OAuth)
- Complete web UI guide
- CLI scripts guide
- Export file format and JSON schemas
- New Central API surface reference
- HTTP API reference with SSE event payloads
- Troubleshooting
- Extending the tool with new exporters

---

## License

MIT
