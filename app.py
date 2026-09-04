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
    if (_project_of(container) or "") in COOLIFY_PROJECTS:
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
_COOLIFY_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<id>[a-z0-9]{12,})$")


def coolify_enabled() -> bool:
    return bool(COOLIFY_API_URL and COOLIFY_API_TOKEN)


def coolify_deploy(project: str) -> dict:
    # Always force: the fallback's whole point is bringing back GC'd/removed
    # containers, where a polite deploy can fail with "deployment in progress"
    # or "nothing to deploy" guards.
    params = urlencode({"uuid": project, "force": "true"})
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
        raise HTTPException(e.code, f"Deploy failed: {detail}") from e
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
        # One Docker call; whitelist is enforced in Python (all=True includes
        # stopped/created, so ALLOWED_NAMES need no per-name lookups).
        by_name = {
            c.name: c for c in client().containers.list(all=True) if is_allowed(c)
        }
    except DockerException as e:
        logger.error("Docker list failed: %s", e)
        return []

    out = [card for c in by_name.values() if (card := _card(c))]
    present = {card["project"] for card in out if card["project"]}
    if coolify_enabled():
        out += [_card_missing(p) for p in COOLIFY_PROJECTS - present]
    out.sort(key=lambda x: x["name"].lower())
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
