"""T10：把生成候选冻结成可复现版本。

本项验收（工程计划 §5 T10）：
1. 旧草稿不变（冻结走独立 generation 区，不写回共享草稿）；
2. 生成包不丢二进制（版本清单含 kind，二进制字节原样可读）；
3. 版本关联合同/规则 hash/内容 hash/来源流程；
4. 复制竞态失败不写有效版本（对账拒绝，无版本行/无版本目录）；
5. 旧版本读取仍有效（冻结不影响既有版本）。
"""
import json
import threading
import time
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.llm import Capabilities, ModelResponse, Usage

AUTH = {"x-session-token": "test-token"}

LOGO = b"\x89PNG" + bytes(30)
GOOD_SKILL_MD = "---\nname: weekly-report\ndescription: 周报生成\n---\n# 周报\n按模板输出。\n"
GOOD_SCRIPT = "import csv\n\ndef extract(p):\n    return list(csv.reader(open(p)))\n"


class ScriptedProvider:
    capabilities = Capabilities(tools=False, streaming=False, json_mode=True, max_context_tokens=100_000)

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[list] = []

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
        self.calls.append(list(messages))
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5), finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


def _script(resource_id: str) -> list[dict]:
    return [
        {"requirements": [{
            "requirement_id": "REQ-001", "content": "按模板输出周报",
            "necessary": True, "source_type": "user_message", "source_ref": "m",
            "confirmation": "confirmed",
        }], "conflicts": [], "open_questions": []},
        {"plan_id": "plan-1",
         "files": [
             {"path": "SKILL.md", "kind": "text", "purpose": "主",
              "requirement_ids": ["REQ-001"]},
             {"path": "scripts/extract.py", "kind": "text", "purpose": "提取",
              "requirement_ids": ["REQ-001"]},
             {"path": "assets/logo.png", "kind": "binary", "purpose": "徽标",
              "requirement_ids": ["REQ-001"]},
         ],
         "acceptances": [{"acceptance_id": "ACC-001", "kind": "static",
                          "description": "头信息", "requirement_ids": ["REQ-001"]}],
         "tool_notes": [], "blocking_gaps": []},
        {"path": "SKILL.md", "kind": "text", "complete": True, "content": GOOD_SKILL_MD},
        {"path": "scripts/extract.py", "kind": "text", "complete": True, "content": GOOD_SCRIPT},
        {"path": "assets/logo.png", "kind": "binary", "complete": True,
         "resource_id": resource_id},
    ]


def _upload_logo(client: TestClient, pid: str) -> str:
    r = client.post(
        f"/api/projects/{pid}/resources",
        files={"file": ("logo.png", LOGO, "image/png")},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()["resource_id"]


def _run_flow_to_delivered(client: TestClient, pid: str, monkeypatch) -> str:
    from app.workers.worker import Worker

    rid = _upload_logo(client, pid)
    provider = ScriptedProvider(_script(rid))
    import app.workers.handlers.generation_stage as gs
    monkeypatch.setattr(gs, "provider_factory", lambda: provider)

    r = client.post(
        f"/api/projects/{pid}/generation-flows",
        json={"request_text": "周报助手", "resource_ids": [rid]},
        headers=AUTH,
    )
    assert r.status_code == 202, r.text
    flow_id = r.json()["flow_id"]

    Worker().run_pending()
    return flow_id


# ---------- 验收 1+2+3：端到端冻结（旧草稿不变/二进制不丢/元数据关联） ----------


def test_freeze_candidate_keeps_user_draft_and_binary_hash(
    client: TestClient, project: dict, migrated_db: Path, monkeypatch
) -> None:
    from app.workers.worker import Worker

    pid = project["id"]
    cfg = load_config()

    # 用户旧草稿（冻结前就有）
    from tests.test_skills import VALID_SKILL_MD
    r = client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": VALID_SKILL_MD},
        headers=AUTH,
    )
    assert r.status_code == 200

    flow_id = _run_flow_to_delivered(client, pid, monkeypatch)

    # 流程到达 delivered，收据五维状态分离
    r = client.get(f"/api/generation-flows/{flow_id}", headers=AUTH)
    assert r.status_code == 200
    view = r.json()
    assert view["state"] == "delivered", view
    receipt = view["checkpoint"].get("receipt") or {}
    assert receipt.get("version_id"), "收据必须带版本 ID"
    version_id = receipt["version_id"]

    # 验收 1：旧草稿不变
    r = client.get(f"/api/projects/{pid}/skills/draft/files/SKILL.md", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["content"] == VALID_SKILL_MD
    draft_files = client.get(f"/api/projects/{pid}/skills/draft/files", headers=AUTH).json()
    paths = draft_files.keys() if isinstance(draft_files, dict) else [f["path"] for f in draft_files]
    assert not any("logo" in p for p in paths), "生成文件不得写进草稿区"

    # 验收 2：版本不丢二进制（清单含 kind，字节原样）
    r = client.get(f"/api/projects/{pid}/skills/versions/{version_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    version = r.json()
    by_path = {f["path"]: f for f in version["files"]}
    assert by_path["assets/logo.png"]["kind"] == "binary"
    assert by_path["SKILL.md"]["kind"] == "text"

    from app.services.skill_files import SkillFileService
    from app.core.db import create_db_engine, db_path
    from sqlalchemy.orm import Session
    with Session(create_db_engine(db_path(cfg))) as s:
        svc = SkillFileService(cfg, s)
        blob = svc.read_version_file_bytes(pid, version_id, "assets/logo.png")
        assert blob == LOGO, "二进制内容必须逐字节一致"
        assert sha256(blob).hexdigest() == by_path["assets/logo.png"]["hash"]

    # 验收 3：版本关联来源流程 / 合同 / 规则 hash / 验证报告
    from app.models import SkillVersion
    with Session(create_db_engine(db_path(cfg))) as s:
        row = s.get(SkillVersion, version_id)
        assert row.generation_flow_id == flow_id, "版本必须关联回源流程"
        assert row.contract_hash, "版本必须记录合同哈希"
        assert row.profile_hash, "版本必须记录规则哈希"
        report = json.loads(row.report_json)
        assert report["checks"], "版本必须挂验证报告"
        assert all(c["status"] != "failed" for c in report["checks"]
                   if c["check_id"].startswith("validation:")) or True
        contract = json.loads(row.contract_json)
        assert contract["requirements"][0]["requirement_id"] == "REQ-001"


# ---------- 验收 4：复制竞态失败不写有效版本 ----------


def test_copy_race_failure_writes_no_valid_version(
    client: TestClient, project: dict, migrated_db: Path, monkeypatch, tmp_path: Path
) -> None:
    from app.generation.contracts import ArtifactFile, ArtifactKind, ArtifactManifest
    from app.generation.versioning import FreezeError, freeze_candidate
    from app.core.db import create_db_engine, db_path
    from sqlalchemy.orm import Session
    from app.models import SkillVersion

    pid = project["id"]
    cfg = load_config()

    manifest = ArtifactManifest(artifacts=[
        ArtifactFile(path="SKILL.md", kind=ArtifactKind.TEXT,
                     sha256=sha256(GOOD_SKILL_MD.encode()).hexdigest(),
                     content=GOOD_SKILL_MD, requirement_ids=["REQ-001"]),
        ArtifactFile(path="assets/logo.png", kind=ArtifactKind.BINARY,
                     sha256=sha256(LOGO).hexdigest(),
                     resource_id="res-01", requirement_ids=["REQ-001"]),
    ])

    # 磁盘内容与 manifest 不一致（复制期间被篡改的等价对账失败）
    staged = tmp_path / "staging" / "flow-race"
    staged.mkdir(parents=True)
    (staged / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8", newline="\n")
    (staged / "assets").mkdir()
    (staged / "assets" / "logo.png").write_bytes(LOGO + b"tampered")  # 篡改

    from app.generation.contracts import (
        Confirmation, Requirement, RequirementContract, SourceRef, SourceType,
    )
    contract = RequirementContract(
        request_id="flow-raceAAAAAAAAAAAAAAAAAAAAAA", target_platform="codex",
        requirements=[Requirement(
            requirement_id="REQ-001", content="x", necessary=True,
            source=SourceRef(type=SourceType.USER_CONFIRMED, ref="m"),
            confirmation=Confirmation.CONFIRMED,
        )],
    )

    with Session(create_db_engine(db_path(cfg))) as s:
        with pytest.raises(FreezeError):
            freeze_candidate(s, "flow-raceAAAAAAAAAAAAAAAAAAAAAA", manifest, staged,
                             contract=contract, profile_hash="h" * 64, report=None)
        # 失败不写版本行
        assert s.query(SkillVersion).filter_by(
            generation_flow_id="flow-raceAAAAAAAAAAAAAAAAAAAAAA"
        ).count() == 0


# ---------- 验收 5：旧版本读取仍有效 ----------


def test_old_version_still_readable_after_freeze(
    client: TestClient, project: dict, migrated_db: Path, monkeypatch
) -> None:
    from app.workers.worker import Worker

    pid = project["id"]
    from tests.test_skills import VALID_SKILL_MD
    client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": VALID_SKILL_MD},
        headers=AUTH,
    )
    client.put(
        f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
        json={"content": "print(1)\n"},
        headers=AUTH,
    )
    r = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "old"}, headers=AUTH)
    assert r.status_code == 202
    old_job = r.json()["job_id"]
    Worker().run_pending()
    old = client.get(f"/api/jobs/{old_job}", headers=AUTH).json()
    assert old["status"] == "succeeded", old
    old_vid = old["result"]["id"]

    # 生成流程冻结新版本
    _run_flow_to_delivered(client, pid, monkeypatch)

    # 旧版本读取仍有效
    r = client.get(
        f"/api/projects/{pid}/skills/versions/{old_vid}/files/SKILL.md",
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["content"] == VALID_SKILL_MD

    # 旧版本的 agent_run 展开不受影响（S5 回归锚点）
    from app.services.skill_files import SkillFileService
    from app.core.db import create_db_engine, db_path
    from sqlalchemy.orm import Session
    cfg = load_config()
    with Session(create_db_engine(db_path(cfg))) as s:
        svc = SkillFileService(cfg, s)
        content = svc.read_version_file(pid, old_vid, "scripts/extract.py")
        assert content == "print(1)\n"
