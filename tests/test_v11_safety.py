"""Regression tests exercise real services; only external models and I/O faults are faked."""
import json
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.executors import ApiExecutor
from app.llm import Capabilities, ModelResponse, ToolCall, Usage
from app.models import Job, Project
from app.services import queue
from app.services.projects import ProjectService
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}


class Responses:
    capabilities = Capabilities(True, False, True, 100000)

    def __init__(self, response):
        self.response = response

    def complete(self, messages, **kwargs):
        return self.response


def test_budget_stops_tool_and_preserves_existing_artifacts(tmp_path):
    (tmp_path / "before.md").write_text("already written")
    provider = Responses(ModelResponse(
        tool_calls=[ToolCall("t", "write_file", {"path": "late.md", "content": "late"})],
        usage=Usage(100, 15), finish_reason="tool_calls",
    ))
    result = ApiExecutor(provider, system_prompt="s", user_prompt="u", sandbox=tmp_path,
                         max_tokens_total=100).run()
    assert result.exit_reason.value == "budget_exceeded"
    assert not (tmp_path / "late.md").exists()
    assert result.sandbox_files["text_contents"]["before.md"] == "already written"


def test_completed_output_records_budget_overrun(tmp_path):
    provider = Responses(ModelResponse(content="done", usage=Usage(100, 15), finish_reason="stop"))
    result = ApiExecutor(provider, system_prompt="s", user_prompt="u", sandbox=tmp_path,
                         max_tokens_total=100).run()
    assert result.exit_reason.value == "completed"
    assert result.budget["over_limit"] is True
    assert result.budget["overrun_tokens"] == 15


def test_cancel_after_model_return_blocks_tools(tmp_path):
    cancelled = threading.Event()
    class CancellingProvider(Responses):
        def complete(self, messages, **kwargs):
            cancelled.set()
            return super().complete(messages, **kwargs)
    provider = CancellingProvider(ModelResponse(
        tool_calls=[ToolCall("t", "write_file", {"path": "late.md", "content": "late"})],
        usage=Usage(1, 1), finish_reason="tool_calls",
    ))
    result = ApiExecutor(provider, system_prompt="s", user_prompt="u", sandbox=tmp_path).run(
        is_cancelled=cancelled.is_set)
    assert result.exit_reason.value == "cancelled"
    assert not (tmp_path / "late.md").exists()


def test_delete_refuses_active_model_job(client, project, monkeypatch):
    import app.workers.handlers.agent_run as module
    entered, release = threading.Event(), threading.Event()
    class BlockingProvider(Responses):
        def complete(self, messages, **kwargs):
            entered.set()
            assert release.wait(10)
            return ModelResponse(content="done", usage=Usage(1, 1), finish_reason="stop")
    monkeypatch.setattr(module, "provider_factory", lambda: BlockingProvider(None))
    pid = project["id"]
    response = client.post(f"/api/projects/{pid}/runs", json={"prompt": "task"}, headers=AUTH)
    jid = response.json()["job_id"]
    errors = []
    def execute():
        try:
            Worker().run_once()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert entered.wait(10)
        response = client.delete(f"/api/projects/{pid}", headers=AUTH)
    finally:
        release.set()
        thread.join(10)
    assert response.status_code == 409
    assert not errors
    assert client.get(f"/api/projects/{pid}", headers=AUTH).status_code == 200
    assert client.get(f"/api/jobs/{jid}", headers=AUTH).json()["status"] == "succeeded"
    assert client.delete(f"/api/projects/{pid}", headers=AUTH).status_code == 204


def test_real_delete_rollback_restores_files_and_queued_jobs(client, project, monkeypatch):
    cfg = load_config()
    engine = create_db_engine(db_path(cfg))
    pid = project["id"]
    path = cfg.data_dir / "projects" / pid / "marker.md"
    path.parent.mkdir(parents=True)
    path.write_text("keep")
    with Session(engine) as session:
        jid = queue.enqueue(session, "sleep", {"project_id": pid, "seconds": 1}).id
    with Session(engine) as session:
        def fail_delete(obj):
            raise RuntimeError("injected failure")
        monkeypatch.setattr(session, "delete", fail_delete)
        with pytest.raises(RuntimeError):
            ProjectService(session).delete(pid)
        session.rollback()
    with Session(engine) as session:
        assert session.get(Project, pid) is not None
        assert session.get(Job, jid).status == "queued"
    assert path.read_text() == "keep"


def test_cleanup_failure_keeps_recoverable_tombstone(client, project, monkeypatch):
    import app.services.projects as module
    cfg = load_config()
    pid = project["id"]
    path = cfg.data_dir / "projects" / pid / "marker.md"
    path.parent.mkdir(parents=True)
    path.write_text("cleanup")
    original = module.shutil.rmtree
    def fail_cleanup(path):
        raise PermissionError("injected file lock")
    monkeypatch.setattr(module.shutil, "rmtree", fail_cleanup)
    response = client.delete(f"/api/projects/{pid}", headers=AUTH)
    assert response.status_code == 204
    assert response.headers["x-cleanup-pending"] == "true"
    assert not path.parent.exists()
    assert list((cfg.data_dir / ".trash").rglob("marker.md"))
    monkeypatch.setattr(module.shutil, "rmtree", original)
    from app.services.projects import reconcile_project_trash
    status = reconcile_project_trash(cfg)
    assert status["removed"] == 1
    assert not list((cfg.data_dir / ".trash").rglob("marker.md"))


def test_crash_before_db_commit_restores_project_files(client, project):
    cfg = load_config()
    pid = project["id"]
    root = cfg.data_dir / "projects" / pid
    root.mkdir(parents=True)
    (root / "marker.md").write_text("restore")
    trash = cfg.data_dir / ".trash"
    trash.mkdir()
    root.rename(trash / f"{pid}.{pid}")
    from app.services.projects import reconcile_project_trash
    assert reconcile_project_trash(cfg)["restored"] == 1
    assert (root / "marker.md").read_text() == "restore"


def test_independent_heartbeat_protects_slow_model(client, project, monkeypatch):
    import app.workers.handlers.agent_run as module
    monkeypatch.setenv("SKILLFORGE_LEASE_SECONDS", "0.3")
    pid = project["id"]
    class SlowProvider(Responses):
        def complete(self, messages, **kwargs):
            time.sleep(0.65)
            assert Worker().recover() == []
            return ModelResponse(content="done", usage=Usage(1, 1), finish_reason="stop")
    monkeypatch.setattr(module, "provider_factory", lambda: SlowProvider(None))
    jid = client.post(f"/api/projects/{pid}/runs", json={"prompt": "task"}, headers=AUTH).json()["job_id"]
    Worker().run_once()
    job = client.get(f"/api/jobs/{jid}", headers=AUTH).json()
    assert job["status"] == "succeeded"
    assert job["result"]["exit_reason"] == "completed"


def test_stale_session_cannot_overwrite_interrupted_job(migrated_db):
    engine = create_db_engine(db_path(load_config()))
    with Session(engine) as session:
        jid = queue.enqueue(session, "sleep", {"seconds": 1}).id
        queue.claim(session, "old", 0.01)
        cached = session.get(Job, jid)
        assert cached.status == "running"
        time.sleep(0.02)
        with Session(engine) as other:
            assert jid in queue.recover_stale(other)
        queue.complete(session, jid, {"late": True})
    with Session(engine) as session:
        job = session.get(Job, jid)
        assert job.status == "interrupted"
        assert job.result_json is None
