# central-group-migration

Export and import HPE Aruba Classic Central UI group configurations (AOS10)
between tenants. Provides a web UI, CLI scripts, and a Docker container for
deployment.

> **Scope:** AOS10 UI groups only. Template groups and pre-AOS10 (Instant)
> groups are not supported.

---

## Quick start

```bash
git clone https://github.com/<your-org>/central-group-migration.git
cd central-group-migration
docker compose up -d
open http://localhost:8000
```

On first run, Docker downloads and installs all dependencies including
`pycentral`. See [§ Docker deployment](#docker-deployment) for details.

---

## What is exported

| File | Contents | Groups |
|------|----------|--------|
| `properties.json` | `allowed_types`, `aos10`, `monitor_only_sw/cx` | All |
| `ap_cli_config.json` | Full AOS10 CLI config blob | IAP groups only |
| `country.json` | RF country code | IAP groups only |

Exported files are written to `exports/<group-name>/` and persist on the
host filesystem via a Docker bind-mount. They are excluded from git by
`.gitignore`.

---

## Project layout

```
central-group-migration/
├── app.py                   # Flask backend (routes, SSE, orchestration)
├── exporters.py             # Per-data-type export/import logic
├── export_groups.py         # CLI export script
├── import_groups.py         # CLI import script
├── templates/
│   └── index.html           # Web UI — Export / Import / Data tabs
├── exports/                 # Runtime data (bind-mounted, git-ignored)
│   └── .gitkeep
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── .github/
│   ├── workflows/ci.yml     # Lint, import check, Docker build + smoke test
│   └── pull_request_template.md
├── README.md                # This file
└── DOCS.md                  # Full technical documentation
```

---

## Docker deployment

```bash
# Build and start (installs pycentral and all deps on first build)
docker compose up -d

# Watch build output — confirms pycentral download from PyPI
docker compose build --progress=plain

# View logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build

# Stop (exports/ data preserved on host)
docker compose down
```

The container runs on port `8000`. Change the host port in
`docker-compose.yml` if needed:

```yaml
ports:
  - "9090:8000"
```

### Verify pycentral is installed in the container

```bash
docker exec central-migration python -c "
import importlib.metadata
for pkg in ['pycentral', 'flask', 'flask-cors', 'gunicorn']:
    print(pkg, importlib.metadata.version(pkg))
"
```

Expected:

```
pycentral 1.4.1
flask 3.1.0
flask-cors 5.0.0
gunicorn 23.0.0
```

---

## Web UI

Three tabs:

**Export** — connect to a source Classic Central instance, select groups,
run export. Progress streams in real time.

**Import** — load an export from disk, connect to a target instance, select
groups, run import. Groups already present on the target are skipped
automatically.

**Data** — browse exported configuration on disk without any API calls.
Includes a searchable group list, property grid, and syntax-highlighted
AOS10 CLI config with a copy button.

---

## CLI scripts

```bash
# Export
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<token> \
python export_groups.py

# Import
CENTRAL_BASE_URL=https://apigw-prod2.central.arubanetworks.com \
CENTRAL_TOKEN=<target-token> \
python import_groups.py
```

The CLI scripts and web UI share the same `exporters.py` logic and the
same `exports/` directory. They are interchangeable.

---

## Authentication

Generate an access token from the Central UI:
**Maintain → Organization → Platform Integration → API Gateway → My Apps & Tokens → Generate Token**

Tokens are valid for 2 hours. For long-running operations, use OAuth
credentials in `ArubaCentralBase` to enable automatic refresh — see
[DOCS.md § Authentication](DOCS.md#6-authentication).

---

## Adding a new data type

All export/import logic is registered in `EXPORTERS` in `exporters.py`.
Add an `export_fn`, an `import_fn`, and one registry entry — no other
files need to change. See [DOCS.md § Extending the Tool](DOCS.md#13-extending-the-tool)
for a worked example.

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pycentral` | 1.4.1 | Aruba Central Python SDK — all API calls |
| `flask` | 3.1.0 | Web framework |
| `flask-cors` | 5.0.0 | CORS headers |
| `gunicorn` | 23.0.0 | Production WSGI server |

pycentral brings its own pinned dependencies (`requests`, `PyYAML`,
`urllib3`, `certifi`) which are installed automatically.

---

## Documentation

Full technical documentation is in **[DOCS.md](DOCS.md)**, covering:

- Architecture and request flow
- Docker build verification (including pycentral install confirmation)
- Authentication and cluster base URLs by region
- Web UI guide for all three tabs
- CLI script guide with example output
- Export file format and JSON schemas
- pycentral SDK reference (`ArubaCentralBase`, `command()`, `Groups`)
- Full HTTP API reference with SSE event payloads
- Troubleshooting (401/403 errors, SSE drops, WARN statuses, proxy config)
- Extending the tool with new exporters

---

## CI

GitHub Actions runs on every push and pull request to `main`:

1. Install dependencies and verify all modules import cleanly
2. Assert all required routes are registered
3. Validate `Dockerfile` syntax with Hadolint
4. Build the Docker image
5. Run a health endpoint smoke test against the built container

---

## License

MIT
