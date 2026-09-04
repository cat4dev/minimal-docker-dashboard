# Container Control UI

Tiny FastAPI + Jinja dashboard: whitelist-only container restart / stop / start + log tail.

## Quick start

1. Configure whitelist via `.env` (recommended) or `docker-compose.yml`:

   ```bash
   cp .env.example .env
   # edit FILTER_LABEL / ALLOWED_CONTAINERS / EXCLUDE_CONTAINERS
   ```

   Labels: one or more `key=value`, comma-separated (**OR**):

   ```env
   FILTER_LABEL=com.docker.compose.project=cat4dev,com.docker.compose.project=core
   ALLOWED_CONTAINERS=
   EXCLUDE_CONTAINERS=container-ui
   ```

2. Deploy:

   ```bash
   docker compose up -d --build
   ```

3. Put it behind reverse-proxy auth (Basic Auth / Authelia / Authentik).  
   **Do not expose port 8080 publicly without auth.**

## Features

- Whitelist by **name** and/or **label(s)** and/or **Coolify project** (`COOLIFY_PROJECTS`; union, multiple projects = OR)
- **Self-protect**: this UI container is never listed or stop/restart/start/logs’d (auto-detect + `EXCLUDE_CONTAINERS`)
- Restart / Stop / Start (enforced server-side)
- Logs modal (last N lines, timestamps)
- Search filter, optional 30s auto-refresh, copy name
- Status badges (running / stopped / restarting / paused)
- Branding: `APP_TITLE`, `APP_SUBTITLE`
- Log length: `LOG_TAIL` (default 200, max 2000)
- **Coolify deploy fallback**: redeploy by compose **project name** through the Coolify API (`COOLIFY_PROJECTS` allowlist); GC-removed projects stay visible as "Missing" cards with a Deploy button
- **No CDN**: local CSS/JS only (no Tailwind runtime / `eval`), basic CSP headers

## Coolify deploy fallback

If Coolify garbage collection removes a project's stopped containers (or their
network), you can redeploy it from the dashboard via the Coolify API. Deploy is
keyed on the **compose project name** (`com.docker.compose.project` label), not
the container name, so it still works when the containers no longer exist.

Set in `.env`:

```env
COOLIFY_API_URL=http://192.168.1.11:8000
COOLIFY_API_TOKEN=your-deploy-only-token
COOLIFY_FORCE=false
COOLIFY_PROJECTS=cat4dev,core
```

- `COOLIFY_PROJECTS` is the deploy **allowlist**: only these project names can be redeployed (`POST /api/v1/deploy?uuid=<project>`).
- It is also a **watchlist**: each project always gets a card. If Docker has no containers for it (GC removed them), the card shows **Missing** with a Deploy button to bring it back.
- The project is read from the container's `coolify.projectName` label (or `com.docker.compose.project`); either is accepted.
- For live projects, every card from that project gets a Deploy button.
- `FILTER_LABEL` is optional and separate: you only need it to *show* containers you don't intend to redeploy, or to OR in extra non-project labels. Projects in `COOLIFY_PROJECTS` are already visible.
- The deploy request is fire-and-forget; use Refresh or auto-refresh to watch the containers come back.

## Security

- Backend re-checks whitelist on every action and logs request
- Socket mount `:ro` is not a write block for the Docker API — real controls are whitelist + proxy auth
- Empty whitelist → empty UI and blocked actions
- Dashboard cannot manage itself (avoids lock-out via Stop/Restart)
- Coolify API token is sent in the `Authorization` header and is never logged

## Troubleshooting

```bash
docker compose logs -f container-ui
docker ps --format "table {{.Names}}\t{{.Status}}"
docker ps --filter "label=project=friend-project"
```

Socket permission errors: run as root (default Dockerfile) or `group_add` the host docker GID.

## Files

| File | Role |
|------|------|
| `app.py` | API + whitelist |
| `templates/dashboard.html` | UI markup |
| `static/app.css` | Styles (offline) |
| `static/app.js` | Client JS (offline) |
| `docker-compose.yml` | Deploy |
| `Dockerfile` | Image |
