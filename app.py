#!/usr/bin/env python3
"""
Container Control UI, whitelist-only restart/stop/start + log tail.
Put behind reverse-proxy auth; do not expose port 8080 publicly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

import docker
from docker.errors import DockerException, NotFound
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Container Control", version="1.0")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

_CSP = (
    "default-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "img-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_HEX64 = re.compile(r"(?:^|[^0-9a-f])([0-9a-f]{64})(?:[^0-9a-f]|$)")
_ACTION_MSG = {"restart": "Restarted", "stop": "Stopped", "start": "Started"}
_ACTIONS = {
    "restart": lambda c: c.restart(timeout=10),
    "stop": lambda c: c.stop(timeout=10),
    "start": lambda c: c.start(),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _csv_set(key: str) -> set[str]:
    return {p.strip() for p in os.getenv(key, "").split(",") if p.strip()}


def _parse_labels(raw: str) -> list[tuple[str, str]]:
    """FILTER_LABEL: comma-separated key=value pairs (OR match)."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            logger.error("FILTER_LABEL entry must be key=value, got %r", part)
            continue
        k, v = (x.strip() for x in part.split("=", 1))
        if k:
            pairs.append((k, v))
        else:
            logger.error("FILTER_LABEL key empty in %r", part)
    return pairs


def _slugify(name: str) -> str:
    """Normalize a project name the same way Coolify does (Str::slug)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _self_ids() -> set[str]:
    """This container's id/name so we never manage ourselves."""
    ids: set[str] = set()
    host = (os.environ.get("HOSTNAME") or "").strip()
    if host:
        ids.add(host)
    try:
        with open("/proc/self/cgroup", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        for m in _HEX64.finditer(text):
            full = m.group(1)
            ids.add(full)
            ids.add(full[:12])
    except OSError:
        pass
    return ids


# --- Config ---
ALLOWED_NAMES = _csv_set("ALLOWED_CONTAINERS")
EXCLUDE_NAMES = _csv_set("EXCLUDE_CONTAINERS")
FILTER_LABELS = _parse_labels(os.getenv("FILTER_LABEL", "").strip())
APP_TITLE = os.getenv("APP_TITLE", "Container Control")
APP_SUBTITLE = os.getenv("APP_SUBTITLE", "Managed access dashboard")
LOG_TAIL = max(1, min(int(os.getenv("LOG_TAIL", "200")), 2000))
COOLIFY_API_URL = os.getenv("COOLIFY_API_URL", "").rstrip("/")
COOLIFY_API_TOKEN = os.getenv("COOLIFY_API_TOKEN", "")
# Coolify compose project names (com.docker.compose.project). Deploy allowlist +
# watchlist: a project here always gets a card, even when its containers are gone.
COOLIFY_PROJECTS = _csv_set("COOLIFY_PROJECTS")
COOLIFY_PROJECTS_SLUGS = {_slugify(p) for p in COOLIFY_PROJECTS}


def _parse_resource_uuids(raw: str) -> dict[str, list[str]]:
    """COOLIFY_RESOURCE_UUIDS: project:uuid1,uuid2;project2:uuid3."""
    mapping: dict[str, list[str]] = {}
    for project_block in raw.split(";"):
        project_block = project_block.strip()
        if not project_block:
            continue
        if ":" not in project_block:
            logger.error("COOLIFY_RESOURCE_UUIDS entry must be project:uuid,uuid, got %r", project_block)
            continue
        name, uuids = project_block.split(":", 1)
        name = name.strip()
        uuids = [u.strip() for u in uuids.split(",") if u.strip()]
        if name and uuids:
            mapping[_slugify(name)] = uuids
    return mapping


# Optional manual override: project name -> list of Coolify resource UUIDs.
# Used when automatic resolution fails or the API token has no read ability.
COOLIFY_RESOURCE_UUIDS = _parse_resource_uuids(os.getenv("COOLIFY_RESOURCE_UUIDS", ""))
SELF_IDS = _self_ids()

if FILTER_LABELS:
    logger.info("FILTER_LABEL (OR): %s", FILTER_LABELS)
if COOLIFY_PROJECTS:
    logger.info("COOLIFY_PROJECTS (deploy/watch): %s", sorted(COOLIFY_PROJECTS))
if SELF_IDS or EXCLUDE_NAMES:
    logger.info("Protected: self=%s exclude=%s", sorted(SELF_IDS)[:4], sorted(EXCLUDE_NAMES))

_client: docker.DockerClient | None = None


def client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def whitelist_ok() -> bool:
    return bool(ALLOWED_NAMES or FILTER_LABELS or COOLIFY_PROJECTS)


def is_protected(container) -> bool:
    name = container.name or ""
    if name in EXCLUDE_NAMES:
        return True
    cid = container.id or ""
    return bool(SELF_IDS & {name, cid, cid[:12]})


# Label keys that identify a Coolify project. Coolify sets coolify.projectName;
# plain docker compose sets com.docker.compose.project.
_PROJECT_LABEL_KEYS = ("coolify.projectName", "com.docker.compose.project")


def _project_of(container) -> str | None:
    labels = container.labels or {}
    for key in _PROJECT_LABEL_KEYS:
        if labels.get(key):
            return labels[key]
    return None


def is_allowed(container) -> bool:
    if is_protected(container):
        return False
    if container.name in ALLOWED_NAMES:
        return True
    labels = container.labels or {}
    container_project = _project_of(container)
    if container_project and _slugify(container_project) in COOLIFY_PROJECTS_SLUGS:
        return True
    if not FILTER_LABELS:
        return False
    return any(labels.get(k) == v for k, v in FILTER_LABELS)


def get_container(name: str):
    if not whitelist_ok():
        raise HTTPException(503, "No whitelist configured")
    if not name or "/" in name or ".." in name:
        raise HTTPException(400, "Invalid container name")

    if name in EXCLUDE_NAMES or name in SELF_IDS:
        logger.warning("Blocked protected container: %s", name)
        raise HTTPException(403, "This dashboard cannot manage its own container")

    try:
        container = client().containers.get(name)
    except NotFound:
        raise HTTPException(404, "Container not found") from None
    except DockerException as e:
        raise HTTPException(502, f"Docker error: {e}") from e

    if is_protected(container):
        logger.warning("Blocked protected container: %s", name)
        raise HTTPException(403, "This dashboard cannot manage its own container")
    if not is_allowed(container):
        logger.warning("Blocked unauthorized container: %s", name)
        raise HTTPException(403, "Not allowed")
    return container


# Coolify appends a long random id: my-app-ae3esvwu63r3yxju2369ywwk
# Coolify UUIDs are 24 lowercase alphanumeric chars. We accept 20+ chars to
# leave a little headroom, but reject plain numeric suffixes like container IDs.
_COOLIFY_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<id>[a-z0-9]{20,})$")


def coolify_enabled() -> bool:
    return bool(COOLIFY_API_URL and COOLIFY_API_TOKEN)


def _coolify_request(path: str, method: str = "GET") -> dict | list:
    """Make an authenticated Coolify API request and return JSON."""
    url = f"{COOLIFY_API_URL}/api/v1{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {COOLIFY_API_TOKEN}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        detail = body or str(e)
        try:
            payload = json.loads(body) if body else {}
            if isinstance(payload, dict):
                detail = payload.get("message") or payload.get("error") or detail
        except json.JSONDecodeError:
            pass
        raise HTTPException(e.code, f"Coolify API error: {detail}") from e
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Deploy API unreachable: {e.reason}") from e


def _coolify_project_uuid(name: str) -> str | None:
    """Resolve a Coolify project name to its UUID."""
    projects = _coolify_request("/projects")
    if not isinstance(projects, list):
        logger.warning("Coolify /projects did not return a list: %s", type(projects).__name__)
        return None
    name_slug = _slugify(name)
    for project in projects:
        if isinstance(project, dict) and _slugify(project.get("name", "")) == name_slug:
            logger.info("Resolved Coolify project '%s' -> uuid %s", name, project.get("uuid"))
            return project.get("uuid")
    logger.warning("Coolify project '%s' not found in %d projects", name, len(projects))
    return None


def _coolify_resource_uuids(project_uuid: str) -> list[str]:
    """Return all deployable resource UUIDs inside a Coolify project."""
    environments = _coolify_request(f"/projects/{quote(project_uuid, safe='')}/environments")
    if not isinstance(environments, list):
        logger.warning(
            "Coolify /projects/%s/environments did not return a list: %s",
            project_uuid,
            type(environments).__name__,
        )
        return []

    uuids: list[str] = []
    resource_keys = (
        "applications",
        "services",
        "postgresqls",
        "redis",
        "mongodbs",
        "mysqls",
        "mariadbs",
    )
    for env in environments:
        if not isinstance(env, dict):
            continue
        env_id = env.get("name") or env.get("uuid")
        if not env_id:
            continue
        details = _coolify_request(
            f"/projects/{quote(project_uuid, safe='')}/{quote(env_id, safe='')}"
        )
        if not isinstance(details, dict):
            logger.warning(
                "Coolify /projects/%s/%s did not return an object: %s",
                project_uuid,
                env_id,
                type(details).__name__,
            )
            continue
        for key in resource_keys:
            for resource in details.get(key, []) or []:
                if isinstance(resource, dict) and resource.get("uuid"):
                    uuids.append(resource["uuid"])
    logger.info("Resolved project uuid %s -> resource uuids: %s", project_uuid, uuids)
    return uuids


def _container_resource_uuids(project: str) -> list[str]:
    """Extract Coolify resource UUIDs from existing container names.

    When containers still exist, we can derive the resource UUIDs directly
    from their names without needing read access to the Coolify projects API.
    """
    try:
        containers = client().containers.list(all=True)
    except DockerException as e:
        logger.warning("Docker list failed during deploy resolution: %s", e)
        return []

    project_slug = _slugify(project)
    protected = EXCLUDE_NAMES | SELF_IDS
    uuids: set[str] = set()
    matched = 0
    for c in containers:
        name = c.name or ""
        if name in protected:
            continue
        labels = c.labels or {}
        container_project = labels.get("coolify.projectName") or labels.get("com.docker.compose.project")
        if not container_project or _slugify(container_project) != project_slug:
            continue
        matched += 1
        m = _COOLIFY_SUFFIX.match(name)
        if m:
            rid = m.group("id")
            # Reject plain numeric IDs (e.g. container short IDs appended by Coolify).
            if re.search(r"[a-z]", rid) and re.search(r"[0-9]", rid):
                uuids.add(rid)
            elif len(rid) >= 24:  # long enough to be a real Coolify UUID
                uuids.add(rid)
    logger.info(
        "Container scan for project '%s': %d matched containers, uuids: %s",
        project,
        matched,
        sorted(uuids),
    )
    return sorted(uuids)


def coolify_deploy(project: str) -> dict:
    # The /deploy endpoint expects Coolify *resource* UUIDs, not the project
    # name. Resolution order:
    #   1. Manual override from COOLIFY_RESOURCE_UUIDS.
    #   2. Live container names (works with a deploy-only API token).
    #   3. Coolify project/environment API (requires read ability).
    project_slug = _slugify(project)
    resource_uuids = COOLIFY_RESOURCE_UUIDS.get(project_slug, [])
    source = "manual"
    if not resource_uuids:
        resource_uuids = _container_resource_uuids(project)
        source = "containers"
    if not resource_uuids:
        project_uuid = _coolify_project_uuid(project)
        if not project_uuid:
            raise HTTPException(404, f"Project '{project}' not found")
        resource_uuids = _coolify_resource_uuids(project_uuid)
        source = "api"

    if not resource_uuids:
        raise HTTPException(404, f"No deployable resources in project '{project}'")

    logger.info("Deploying project '%s' via %s with uuids: %s", project, source, resource_uuids)
    params = urlencode({"uuid": ",".join(resource_uuids), "force": "true"})
    url = f"{COOLIFY_API_URL}/api/v1/deploy?{params}"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {COOLIFY_API_TOKEN}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return {"ok": True, "status": resp.status}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        detail = body or str(e)
        try:
            payload = json.loads(body) if body else {}
            if isinstance(payload, dict):
                detail = payload.get("message") or payload.get("error") or detail
        except json.JSONDecodeError:
            pass
        # The frontend already prefixes its own "Deploy failed:" label.
        raise HTTPException(e.code, detail) from e
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Deploy API unreachable: {e.reason}") from e


def display_name(name: str) -> str:
    """Short label for UI; Docker still uses the full name for actions."""
    m = _COOLIFY_SUFFIX.match(name or "")
    if not m:
        return name
    rid = m.group("id")
    # Random Coolify ids mix letters+digits; skip ordinary words (e.g. "production")
    if re.search(r"[a-z]", rid) and re.search(r"[0-9]", rid):
        return m.group("base")
    if len(rid) >= 20:  # long pure alpha/digit suffix
        return m.group("base")
    return name


def short_image(image: str) -> str:
    """Prefer image:tag without registry path noise when possible."""
    if not image or image == "unknown":
        return image
    # sha256:… digests
    if image.startswith("sha256:"):
        return image[:19] + "…"
    # registry/path/name:tag → name:tag
    if "/" in image:
        image = image.rsplit("/", 1)[-1]
    return image


def _card(c) -> dict | None:
    try:
        try:
            tags = c.image.tags or []
            image = tags[0] if tags else (c.image.short_id or "unknown")
        except Exception:
            image = "unknown"
        state = (c.attrs or {}).get("State") or {}
        status = c.status or "unknown"
        full = c.name
        project = _project_of(c)
        return {
            "name": full,  # full Docker name (forms / API)
            "display_name": display_name(full),
            "project": project,
            "deployable": project in COOLIFY_PROJECTS,
            "status": status,
            "running": status.lower() == "running",
            "image": short_image(image),
            "image_full": image,
            "health": (state.get("Health") or {}).get("Status"),
            "exit_code": state.get("ExitCode"),
        }
    except Exception as e:
        logger.warning("Skipping %s: %s", getattr(c, "name", "?"), e)
        return None


def _card_missing(project: str) -> dict:
    """Placeholder for a COOLIFY_PROJECTS project Docker can no longer find
    (e.g. its containers were removed by Coolify GC), kept visible so its
    Deploy button survives."""
    return {
        "name": project,
        "display_name": display_name(project),
        "project": project,
        "deployable": True,
        "status": "missing",
        "running": False,
        "image": "",
        "image_full": "",
        "health": None,
        "exit_code": None,
    }


def list_allowed() -> list[dict]:
    if not whitelist_ok():
        logger.warning("No whitelist configured — empty list")
        return []

    try:
        all_containers = client().containers.list(all=True)
    except DockerException as e:
        logger.error("Docker list failed: %s", e)
        return []

    # Debug logging: show every container Docker sees and why it is filtered.
    for c in all_containers:
        labels = c.labels or {}
        project = _project_of(c)
        reason = "allowed"
        if is_protected(c):
            reason = f"protected (exclude/self)"
        elif c.name in ALLOWED_NAMES:
            reason = "allowed by name"
        elif project and _slugify(project) in COOLIFY_PROJECTS_SLUGS:
            reason = f"allowed by project '{project}'"
        elif FILTER_LABELS and any(labels.get(k) == v for k, v in FILTER_LABELS):
            reason = "allowed by label"
        else:
            reason = "not whitelisted"
        logger.info(
            "Docker container: name=%s status=%s project_label=%s -> %s",
            c.name,
            c.status,
            project,
            reason,
        )

    by_name = {c.name: c for c in all_containers if is_allowed(c)}
    out = [card for c in by_name.values() if (card := _card(c))]
    present = {card["project"] for card in out if card["project"]}
    if coolify_enabled():
        out += [_card_missing(p) for p in COOLIFY_PROJECTS - present]
    out.sort(key=lambda x: x["name"].lower())
    logger.info("list_allowed: %d Docker containers, %d cards", len(all_containers), len(out))
    return out


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "containers": list_allowed(),
            "message": message,
            "msg_error": message.lower().startswith("error"),
            "app_title": APP_TITLE,
            "app_subtitle": APP_SUBTITLE,
            "log_tail": LOG_TAIL,
            "coolify_enabled": coolify_enabled(),
        },
    )


@app.post("/action")
async def action(container: str = Form(...), action: str = Form(...)):
    name, act = container.strip(), action.strip().lower()
    run = _ACTIONS.get(act)
    if not run:
        msg = "Error: Unknown action"
    else:
        try:
            run(get_container(name))
            msg = f"{_ACTION_MSG[act]} {name}"
        except HTTPException as e:
            msg = f"Error: {e.detail}"
        except DockerException as e:
            logger.error("Action failed: %s", e)
            msg = f"Error: {e}"
    return RedirectResponse(url=f"/?message={quote(msg, safe='')}", status_code=303)


@app.post("/api/deploy/{project}")
async def api_deploy(project: str):
    if not coolify_enabled():
        raise HTTPException(503, "Deploy fallback is not configured")
    if not project or "/" in project or ".." in project:
        raise HTTPException(400, "Invalid project name")
    if project not in COOLIFY_PROJECTS:
        logger.warning("Blocked deploy for unauthorized project: %s", project)
        raise HTTPException(403, "Project not in deploy allowlist")
    try:
        result = coolify_deploy(project)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Coolify deploy failed: %s", e)
        raise HTTPException(502, f"Deploy failed: {e}") from e
    return {"project": project, "result": result}


@app.get("/api/logs/{name}")
async def api_logs(name: str, tail: int = LOG_TAIL):
    c = get_container(name)
    try:
        n = max(1, min(int(tail), 2000))
    except ValueError:
        n = LOG_TAIL
    try:
        raw = c.logs(tail=n, timestamps=True)
    except DockerException as e:
        raise HTTPException(502, f"Docker error: {e}") from e
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    return {"container": name, "logs": text}


@app.get("/health")
async def health():
    try:
        client().ping()
        ok = True
    except Exception as e:
        logger.warning("Docker ping failed: %s", e)
        ok = False
    return {
        "status": "ok" if ok else "degraded",
        "docker": ok,
        "whitelist": whitelist_ok(),
        "coolify": coolify_enabled(),
    }


if __name__ == "__main__":
    # Self-check for deploy resolution helpers.
    _orig_request = _coolify_request

    def _fake_request(path: str, method: str = "GET") -> dict | list:
        assert method == "GET", method
        if path == "/projects":
            return [
                {"uuid": "proj-uuid-1", "name": "cat4dev"},
                {"uuid": "proj-uuid-2", "name": "core"},
            ]
        if path == "/projects/proj-uuid-1/environments":
            return [{"uuid": "env-1", "name": "production"}]
        if path == "/projects/proj-uuid-1/production":
            return {
                "applications": [{"uuid": "app-uuid-1"}],
                "services": [],
                "postgresqls": [{"uuid": "db-uuid-1"}],
            }
        raise AssertionError(f"unexpected path: {path}")

    _coolify_request = _fake_request  # type: ignore[assignment]
    assert _coolify_project_uuid("cat4dev") == "proj-uuid-1"
    assert _coolify_project_uuid("CORE") == "proj-uuid-2"
    assert _coolify_project_uuid("missing") is None
    assert _coolify_resource_uuids("proj-uuid-1") == ["app-uuid-1", "db-uuid-1"]
    _coolify_request = _orig_request  # type: ignore[assignment]
    print("self-check ok")
