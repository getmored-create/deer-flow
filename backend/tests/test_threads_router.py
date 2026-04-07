from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.gateway.routers import threads
from deerflow.config.paths import Paths


def test_delete_thread_data_removes_thread_directory(tmp_path):
    paths = Paths(tmp_path)
    thread_dir = paths.thread_dir("thread-cleanup")
    workspace = paths.sandbox_work_dir("thread-cleanup")
    uploads = paths.sandbox_uploads_dir("thread-cleanup")
    outputs = paths.sandbox_outputs_dir("thread-cleanup")

    for directory in [workspace, uploads, outputs]:
        directory.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    (uploads / "report.pdf").write_bytes(b"pdf")
    (outputs / "result.json").write_text("{}", encoding="utf-8")

    assert thread_dir.exists()

    response = threads._delete_thread_data("thread-cleanup", paths=paths)

    assert response.success is True
    assert not thread_dir.exists()


def test_delete_thread_data_is_idempotent_for_missing_directory(tmp_path):
    paths = Paths(tmp_path)

    response = threads._delete_thread_data("missing-thread", paths=paths)

    assert response.success is True
    assert not paths.thread_dir("missing-thread").exists()


def test_delete_thread_data_rejects_invalid_thread_id(tmp_path):
    paths = Paths(tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        threads._delete_thread_data("../escape", paths=paths)

    assert exc_info.value.status_code == 422
    assert "Invalid thread_id" in exc_info.value.detail


def test_delete_thread_route_cleans_thread_directory(tmp_path):
    """DELETE /{thread_id} requires auth + permission — mock auth and store."""
    from unittest.mock import AsyncMock, MagicMock

    from app.gateway.authz import AuthContext

    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    paths = Paths(tmp_path)
    thread_dir = paths.thread_dir(tid)
    paths.sandbox_work_dir(tid).mkdir(parents=True, exist_ok=True)
    (paths.sandbox_work_dir(tid) / "notes.txt").write_text("hello", encoding="utf-8")

    # Mock store item with .value attribute
    mock_store = MagicMock()
    mock_record = {
        "thread_id": tid,
        "metadata": {"user_id": "test-user-123"},
    }
    mock_store_item = MagicMock()
    mock_store_item.value = mock_record
    mock_store.aget = AsyncMock(return_value=mock_store_item)

    mock_user = MagicMock()
    mock_user.id = "test-user-123"
    mock_auth = AuthContext(user=mock_user, permissions=["threads:read", "threads:write", "threads:delete"])

    app = FastAPI()
    app.include_router(threads.router)

    with patch("app.gateway.routers.threads.get_paths", return_value=paths):
        with patch("app.gateway.routers.threads.get_store", return_value=mock_store):
            with patch("app.gateway.routers.threads.get_checkpointer", return_value=MagicMock()):
                with patch("app.gateway.authz._authenticate", return_value=mock_auth):
                    with TestClient(app) as client:
                        response = client.delete(f"/api/threads/{tid}")

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": f"Deleted local thread data for {tid}"}
    assert not thread_dir.exists()


def test_delete_thread_route_rejects_invalid_thread_id(tmp_path):
    paths = Paths(tmp_path)

    app = FastAPI()
    app.include_router(threads.router)

    with patch("app.gateway.routers.threads.get_paths", return_value=paths):
        with TestClient(app) as client:
            response = client.delete("/api/threads/../escape")

    assert response.status_code == 404


def test_delete_thread_route_returns_422_for_route_safe_invalid_id(tmp_path):
    """DELETE /{thread_id} with non-UUID id — FastAPI rejects at path validation."""
    paths = Paths(tmp_path)

    app = FastAPI()
    app.include_router(threads.router)

    with patch("app.gateway.routers.threads.get_paths", return_value=paths):
        with TestClient(app) as client:
            response = client.delete("/api/threads/thread.with.dot")

    assert response.status_code == 422
    # FastAPI returns a list of validation errors for path parameter mismatch
    detail = response.json()["detail"]
    assert any("thread_id" in str(err) for err in detail)


def test_delete_thread_data_returns_generic_500_error(tmp_path):
    paths = Paths(tmp_path)

    with (
        patch.object(paths, "delete_thread_dir", side_effect=OSError("/secret/path")),
        patch.object(threads.logger, "exception") as log_exception,
    ):
        with pytest.raises(HTTPException) as exc_info:
            threads._delete_thread_data("thread-cleanup", paths=paths)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to delete local thread data."
    assert "/secret/path" not in exc_info.value.detail
    log_exception.assert_called_once_with("Failed to delete thread data for %s", "thread-cleanup")
