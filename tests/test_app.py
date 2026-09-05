"""Tests for Coolify deploy resolution.

Run with: pytest tests/test_app.py
"""

import os
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

os.environ.setdefault("COOLIFY_API_URL", "http://coolify.example.com:8000")
os.environ.setdefault("COOLIFY_API_TOKEN", "test-token")
os.environ.setdefault("COOLIFY_PROJECTS", "cat4dev")

from app import (
    _container_resource_uuids,
    _coolify_project_uuid,
    _coolify_resource_uuids,
    _parse_resource_uuids,
    _slugify,
    coolify_deploy,
)


def _fake_coolify_request(path: str, method: str = "GET") -> dict | list:
    assert method == "GET"
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
            "services": [{"uuid": "svc-uuid-1"}],
            "postgresqls": [{"uuid": "db-uuid-1"}],
            "redis": [],
            "mongodbs": [],
            "mysqls": [],
            "mariadbs": [],
        }
    # Simulate empty project / unknown project.
    if "/environments" in path:
        return []
    raise AssertionError(f"unexpected path: {path}")


def test_slugify_normalizes_names():
    assert _slugify("Cat4Dev") == "cat4dev"
    assert _slugify("cat4dev") == "cat4dev"
    assert _slugify(" Cat4Dev ") == "cat4dev"


def test_parse_resource_uuids():
    raw = "cat4dev:uuid-1,uuid-2; core: uuid-3 ; badentry"
    mapping = _parse_resource_uuids(raw)
    assert mapping == {
        "cat4dev": ["uuid-1", "uuid-2"],
        "core": ["uuid-3"],
    }


def test_coolify_project_uuid_resolution(monkeypatch):
    monkeypatch.setattr("app._coolify_request", _fake_coolify_request)
    assert _coolify_project_uuid("cat4dev") == "proj-uuid-1"
    assert _coolify_project_uuid("CORE") == "proj-uuid-2"
    assert _coolify_project_uuid("missing") is None


def test_coolify_resource_uuids(monkeypatch):
    monkeypatch.setattr("app._coolify_request", _fake_coolify_request)
    assert _coolify_resource_uuids("proj-uuid-1") == [
        "app-uuid-1",
        "svc-uuid-1",
        "db-uuid-1",
    ]
    assert _coolify_resource_uuids("unknown-uuid") == []


def test_container_resource_uuids_extracts_from_names(monkeypatch):
    fake_container = MagicMock()
    fake_container.name = "web-app-ae3esvwu63r3yxju2369ywwk"
    fake_container.labels = {"coolify.projectName": "cat4dev"}

    fake_container2 = MagicMock()
    fake_container2.name = "worker-app-ae3esvwu63r3yxju2369ywwk"  # same resource
    fake_container2.labels = {"coolify.projectName": "cat4dev"}

    fake_other = MagicMock()
    fake_other.name = "other-bf4ftwxv74r4zxkv3470zxxl"
    fake_other.labels = {"coolify.projectName": "core"}

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [fake_container, fake_container2, fake_other]
    monkeypatch.setattr("app._client", fake_client)

    assert _container_resource_uuids("cat4dev") == ["ae3esvwu63r3yxju2369ywwk"]


def test_container_resource_uuids_falls_back_to_compose_label(monkeypatch):
    fake_container = MagicMock()
    fake_container.name = "cat4dev_web_1"  # no Coolify suffix
    fake_container.labels = {"com.docker.compose.project": "cat4dev"}

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [fake_container]
    monkeypatch.setattr("app._client", fake_client)

    assert _container_resource_uuids("cat4dev") == []


def test_container_resource_uuids_rejects_numeric_suffix(monkeypatch):
    fake_container = MagicMock()
    fake_container.name = "cat4dev-120010938234"  # numeric, not a Coolify UUID
    fake_container.labels = {"coolify.projectName": "cat4dev"}

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [fake_container]
    monkeypatch.setattr("app._client", fake_client)

    assert _container_resource_uuids("cat4dev") == []


def test_container_resource_uuids_skips_protected_containers(monkeypatch):
    fake_dashboard = MagicMock()
    fake_dashboard.name = "container-ui-idxv1o12dfa23r3eyljabpfij7-120010938234"
    fake_dashboard.labels = {"coolify.projectName": "cat4dev"}

    fake_client = MagicMock()
    fake_client.containers.list.return_value = [fake_dashboard]
    monkeypatch.setattr("app._client", fake_client)
    monkeypatch.setattr("app.EXCLUDE_NAMES", {"container-ui"})

    assert _container_resource_uuids("cat4dev") == []


def test_coolify_deploy_uses_manual_override(monkeypatch):
    monkeypatch.setattr(
        "app.COOLIFY_RESOURCE_UUIDS", {"cat4dev": ["manual-uuid-1"]}
    )
    monkeypatch.setattr("app.COOLIFY_API_URL", "http://coolify.example.com:8000")
    monkeypatch.setattr("app.COOLIFY_API_TOKEN", "test-token")

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = b'{"deployments": [{"resource_uuid": "manual-uuid-1"}]}'
    fake_ctx = MagicMock()
    fake_ctx.__enter__ = MagicMock(return_value=fake_resp)
    fake_ctx.__exit__ = MagicMock(return_value=False)

    called_urls = []

    def fake_urlopen(req, timeout=None):
        called_urls.append(req.full_url)
        return fake_ctx

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = coolify_deploy("cat4dev")

    assert len(called_urls) == 1
    assert "uuid=manual-uuid-1" in called_urls[0]
    assert result == {"deployments": [{"resource_uuid": "manual-uuid-1"}]}


def test_coolify_deploy_uses_container_uuids(monkeypatch):
    monkeypatch.setattr(
        "app._container_resource_uuids", lambda _project: ["res-uuid-1", "res-uuid-2"]
    )
    monkeypatch.setattr("app.COOLIFY_API_URL", "http://coolify.example.com:8000")
    monkeypatch.setattr("app.COOLIFY_API_TOKEN", "test-token")

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = b'{"deployments": [{"resource_uuid": "res-uuid-1"}]}'
    fake_ctx = MagicMock()
    fake_ctx.__enter__ = MagicMock(return_value=fake_resp)
    fake_ctx.__exit__ = MagicMock(return_value=False)

    called_urls = []

    def fake_urlopen(req, timeout=None):
        called_urls.append(req.full_url)
        return fake_ctx

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = coolify_deploy("cat4dev")

    assert len(called_urls) == 1
    url = called_urls[0]
    assert "uuid=res-uuid-1%2Cres-uuid-2" in url or "uuid=res-uuid-1,res-uuid-2" in url
    assert "force=true" in url
    assert result == {"deployments": [{"resource_uuid": "res-uuid-1"}]}


def test_coolify_deploy_propagates_coolify_error(monkeypatch):
    monkeypatch.setattr(
        "app._container_resource_uuids", lambda _project: ["res-uuid-1"]
    )
    monkeypatch.setattr("app.COOLIFY_API_URL", "http://coolify.example.com:8000")
    monkeypatch.setattr("app.COOLIFY_API_TOKEN", "test-token")

    fake_error = HTTPError(
        "http://coolify.example.com:8000/api/v1/deploy",
        404,
        "Not Found",
        {},
        None,
    )
    fake_error.read = MagicMock(return_value=b'{"message": "No resources found."}')

    def fake_urlopen_error(*_args, **_kwargs):
        raise fake_error

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen_error)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        coolify_deploy("cat4dev")
    assert exc_info.value.status_code == 404
    assert "No resources found" in exc_info.value.detail
