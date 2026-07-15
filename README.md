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

- Whitelist by **name** and/or **label(s)** (union; multiple labels = OR)
- **Self-protect**: this UI container is never listed or stop/restart/start/logs’d (auto-detect + `EXCLUDE_CONTAINERS`)
- Restart / Stop / Start (enforced server-side)
- Logs modal (last N lines, timestamps)
- Search filter, optional 30s auto-refresh, copy name
- Status badges (running / stopped / restarting / paused)
- Branding: `APP_TITLE`, `APP_SUBTITLE`
- Log length: `LOG_TAIL` (default 200, max 2000)
- **No CDN**: local CSS/JS only (no Tailwind runtime / `eval`), basic CSP headers

## Security

- Backend re-checks whitelist on every action and logs request
- Socket mount `:ro` is not a write block for the Docker API — real controls are whitelist + proxy auth
- Empty whitelist → empty UI and blocked actions
- Dashboard cannot manage itself (avoids lock-out via Stop/Restart)

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
