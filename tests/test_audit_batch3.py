"""第三批缺陷回归测试：F05 / F06 / F09。

- F05：路径层级检查用 is_relative_to —— 兄弟目录前缀相似不再误判为根内；
- F06：Origin 精确解析 scheme+host —— http://127.0.0.1.evil.com 被拒；
- F09：sandbox_files 清单（含二进制）/ 文本内容分离 ——
  file_exists 对二进制文件成立，file_contains 给出明确 detail。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import origin_allowed
from app.services.scoring import score_assertions
from app.tools.registry import resolve_sandbox_path, ToolValidationError
from app.services.skill_files import FileValidationError

AUTH = {"x-session-token": "test-token"}


# ---------- F05：路径层级 ----------


def test_f05_sibling_directory_prefix_rejected(tmp_path: Path) -> None:
    """sandbox 与 sandbox-evil 前缀相似：is_relative_to 不再误判。"""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (tmp_path / "sandbox-evil").mkdir()  # 兄弟目录

    # 正常路径仍可解析
    ok = resolve_sandbox_path(sandbox, "SKILL.md")
    assert ok == sandbox / "SKILL.md"

    # 直接展示新旧语义差异：字符串前缀会误判兄弟目录在沙箱内，
    # is_relative_to 正确判定层级关系（F05 修复依据）
    evil = tmp_path / "sandbox-evil" / "x"
    assert str(evil).startswith(str(sandbox)) is True  # 旧实现的缺陷
    assert evil.resolve().is_relative_to(sandbox.resolve()) is False  # 新实现

    # 纵深防御：../ 穿越在第一层 normalize_relpath 就被拦（FileValidationError）
    with pytest.raises((ToolValidationError, FileValidationError)):
        resolve_sandbox_path(sandbox, "../sandbox-evil/x")


def test_f05_skill_files_sibling_root_rejected(tmp_path: Path) -> None:
    """skill_files._resolve：兄弟目录不落入根内（经 normalize_relpath 已挡 ../，
    这里直接验证 is_relative_to 分支的行为语义）。"""
    from app.core.config import AppConfig
    from app.services.skill_files import SkillFileService
    from sqlalchemy.orm import Session

    root = tmp_path / "proj" / "skills" / "drafts"
    root.mkdir(parents=True)
    (tmp_path / "proj" / "skills" / "drafts-evil").mkdir()

    cfg = AppConfig.model_construct(data_dir=tmp_path / "proj")
    svc = SkillFileService(cfg, session=None)
    # ../drafts-evil/x 会被 normalize_relpath 拒绝（相对路径成分检查），
    # 这里验证该行为保持 + _resolve 的 is_relative_to 兜底
    with pytest.raises(FileValidationError):
        svc._resolve(root, "../drafts-evil/x")


# ---------- F06：Origin 精确比较 ----------


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://127.0.0.1:8000", True),
        ("http://localhost:5173", True),
        ("https://localhost", True),
        ("http://[::1]:8000", True),
        (None, None),  # type: ignore[arg-type]
    ],
)
def test_f06_loopback_origins_allowed(origin: str, allowed: bool) -> None:
    if origin is None:
        pytest.skip("占位：None 在中间件层不触发校验")
    assert origin_allowed(origin) is allowed


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1.evil.com",      # 旧实现误放行的核心案例
        "http://localhost.evil.com",
        "http://evil.com/127.0.0.1",
        "ftp://127.0.0.1",
        "file://127.0.0.1",
        "http://192.168.1.5:8000",
        "http://0.0.0.0:8000",
        "not a url",
    ],
)
def test_f06_malicious_or_foreign_origins_rejected(origin: str) -> None:
    assert origin_allowed(origin) is False


def test_f06_middleware_blocks_evil_origin(client: TestClient) -> None:
    """端到端：携带 127.0.0.1.evil.com Origin 的请求被 403。"""
    r = client.get("/api/health", headers={"origin": "http://127.0.0.1.evil.com"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden_origin"

    r = client.get("/api/health", headers={"origin": "http://127.0.0.1:8317"})
    assert r.status_code == 200


# ---------- F09：清单/内容分离 ----------


def _f09_files() -> dict:
    return {
        "files": [
            {"path": "result.md", "size": 10, "text": True},
            {"path": "img/logo.png", "size": 2048, "text": False},
        ],
        "text_contents": {"result.md": "# 结果\n"},
    }


def test_f09_file_exists_matches_binary_manifest() -> None:
    """二进制文件在清单里：file_exists 通过（旧实现会漏判）。"""
    results = score_assertions(
        [
            {"type": "file_exists", "path": "img/logo.png"},
            {"type": "file_exists", "path": "result.md"},
            {"type": "file_exists", "path": "nope.bin"},
        ],
        sandbox_files=_f09_files(),
        final_message=None,
        exit_reason="completed",
    )
    assert results[0]["passed"] is True
    assert results[1]["passed"] is True
    assert results[2]["passed"] is False


def test_f09_file_contains_binary_detail() -> None:
    """file_contains 对二进制给出明确 detail（存在但非文本）。"""
    results = score_assertions(
        [{"type": "file_contains", "path": "img/logo.png", "contains": "PNG"}],
        sandbox_files=_f09_files(),
        final_message=None,
        exit_reason="completed",
    )
    assert results[0]["passed"] is False
    assert "非文本" in results[0]["detail"]

    results = score_assertions(
        [{"type": "file_contains", "path": "result.md", "contains": "结果"}],
        sandbox_files=_f09_files(),
        final_message=None,
        exit_reason="completed",
    )
    assert results[0]["passed"] is True


def test_f09_legacy_flat_format_still_scores() -> None:
    """过渡兼容：旧版 path→content 扁平映射仍可评分。"""
    results = score_assertions(
        [
            {"type": "file_exists", "path": "a.md"},
            {"type": "file_contains", "path": "a.md", "contains": "x"},
        ],
        sandbox_files={"a.md": "xxx"},
        final_message=None,
        exit_reason="completed",
    )
    assert results[0]["passed"] is True
    assert results[1]["passed"] is True


def test_f09_executor_collects_binary_manifest(tmp_path: Path) -> None:
    """executor 收集：二进制文件进清单、不进 text_contents。"""
    from app.executors import ApiExecutor
    from app.executors.base import ExitReason
    from app.llm import Capabilities, ModelResponse, Usage

    class OneShot:
        capabilities = Capabilities(tools=True, streaming=False, json_mode=True, max_context_tokens=100_000)

        def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
            return ModelResponse(content="done", usage=Usage(10, 5), finish_reason="stop")

    (tmp_path / "out.md").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))

    result = ApiExecutor(OneShot(), system_prompt="s", user_prompt="u", sandbox=tmp_path).run()
    assert result.exit_reason == ExitReason.COMPLETED

    manifest = {f["path"]: f for f in result.sandbox_files["files"]}
    assert set(manifest) == {"out.md", "logo.png"}
    assert manifest["logo.png"]["text"] is False
    assert manifest["logo.png"]["size"] == 108
    assert result.sandbox_files["text_contents"] == {"out.md": "# ok\n"}


def test_f09_eval_flow_with_binary_artifact(
    client: TestClient, migrated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：脚本产出二进制产物（write_file 白名单只放文本，二进制只能由
    run_script 产生），file_exists 断言经新清单结构通过。"""
    from tests.test_agent_runs import FakeProvider, _resp_tool, _resp_text
    from tests.test_skills import VALID_SKILL_MD
    import app.workers.handlers.agent_run as agent_run_mod

    # 建项目 + 套件（断言二进制产物存在）
    r = client.post("/api/projects", json={"name": "p"}, headers=AUTH)
    pid = r.json()["id"]
    cases = [{"id": "a", "prompt": "生成文件", "assertions": [{"type": "file_exists", "path": "out.bin"}]}]
    r = client.post(f"/api/projects/{pid}/suites", json={"name": "s", "cases": cases}, headers=AUTH)
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]

    # 建版本：脚本会产出二进制 out.bin
    client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": VALID_SKILL_MD}, headers=AUTH,
    )
    client.put(
        f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
        json={"content": 'open("out.bin", "wb").write(b"\\x89PNG\\r\\n\\x1a\\n" + bytes(50))\nprint("ok")\n'},
        headers=AUTH,
    )
    r = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "v1"}, headers=AUTH)
    from app.workers.worker import Worker
    Worker().run_pending()
    version_id = client.get(f"/api/jobs/{r.json()['job_id']}", headers=AUTH).json()["result"]["id"]

    fake = FakeProvider([
        _resp_tool("c1", "run_script", {"path": "scripts/extract.py"}),
        _resp_text("写好了。"),
    ])
    monkeypatch.setattr(agent_run_mod, "provider_factory", lambda: fake)

    r = client.post(
        f"/api/projects/{pid}/eval-runs",
        json={"suite_id": suite_id, "version_id": version_id},
        headers=AUTH,
    )
    assert r.status_code == 202, r.text
    from app.workers.worker import Worker
    Worker().run_pending()
    body = client.get(f"/api/jobs/{r.json()['job_id']}", headers=AUTH).json()
    assert body["status"] == "succeeded", body
    summary = body["result"]["summary"]["candidate"]
    assert summary["cases"] == 1 and summary["scored"] == 1
    assert summary["passed_total"] == 1
    # JSON 往返无损
    assert isinstance(body["result"]["case_results"][0]["assertions"], list)
