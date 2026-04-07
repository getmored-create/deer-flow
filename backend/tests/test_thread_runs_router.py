"""Tests for thread_runs router with auth decorators.

These tests verify that auth decorators properly enforce permission checks
on run endpoints. They follow the same pattern as test_threads_router.py.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.auth.models import User
from app.gateway.authz import AuthContext
from app.gateway.routers.thread_runs import router


def test_create_run_requires_auth():
    """POST /{thread_id}/runs requires auth."""
    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/threads/test-thread/runs",
            json={"assistant_id": "test"},
        )
        assert response.status_code == 401


def test_create_run_with_auth():
    """POST /{thread_id}/runs with valid auth passes through."""
    app = FastAPI()
    app.include_router(router)

    mock_user = User(id=uuid4(), email="test@example.com", password_hash="hash")
    mock_auth = AuthContext(
        user=mock_user,
        permissions=["runs:create", "threads:read", "threads:write"],
    )

    # Mock the checkpointer and run_manager to avoid 503s
    mock_checkpointer = MagicMock()
    mock_run_manager = MagicMock()
    mock_run_manager.list_by_thread = MagicMock(return_value=[])
    mock_stream_bridge = MagicMock()

    with patch("app.gateway.routers.thread_runs.get_checkpointer", return_value=mock_checkpointer):
        with patch("app.gateway.routers.thread_runs.get_run_manager", return_value=mock_run_manager):
            with patch("app.gateway.routers.thread_runs.get_stream_bridge", return_value=mock_stream_bridge):
                with patch("app.gateway.authz._authenticate", return_value=mock_auth):
                    with TestClient(app, raise_server_exceptions=False) as client:
                        # Without a real checkpointer.setup, this will 500 - but the point is auth passed
                        response = client.post(
                            "/api/threads/test-thread/runs",
                            json={"assistant_id": "test"},
                        )
                        # Auth passed if we don't get 401
                        assert response.status_code != 401


def test_list_runs_requires_auth():
    """GET /{thread_id}/runs requires auth."""
    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/threads/test-thread/runs")
        assert response.status_code == 401


def test_list_runs_with_auth():
    """GET /{thread_id}/runs with auth passes through."""
    app = FastAPI()
    app.include_router(router)

    mock_user = User(id=uuid4(), email="test@example.com", password_hash="hash")
    mock_auth = AuthContext(
        user=mock_user,
        permissions=["runs:read", "threads:read"],
    )

    mock_run_manager = MagicMock()
    mock_run_manager.list_by_thread = MagicMock(return_value=[])

    with patch("app.gateway.routers.thread_runs.get_run_manager", return_value=mock_run_manager):
        with patch("app.gateway.authz._authenticate", return_value=mock_auth):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/threads/test-thread/runs")
                # Should not be 401 (may be 500 or other, but auth passed)
                assert response.status_code != 401


def test_get_run_requires_auth():
    """GET /{thread_id}/runs/{run_id} requires auth."""
    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/threads/test-thread/runs/run-123")
        assert response.status_code == 401


def test_cancel_run_requires_auth():
    """POST /{thread_id}/runs/{run_id}/cancel requires auth."""
    app = FastAPI()
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/threads/test-thread/runs/run-123/cancel")
        assert response.status_code == 401
