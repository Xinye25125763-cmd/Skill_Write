"""T16：打包、完整验证收据和导出接口。

本项验收（工程计划 §5 T16）：
1. 解包后所有必需文件存在且 sha256 与清单一致（哈希由系统计算，非模型自报）；
2. README 说明依赖与使用（含未验证候选警示，不得当作验收通过）；
3. missing/not_run 检查存在时收据绝不出现"全通过"（delivery_status=candidate，
   fully_verified=False，notes 如实列出未通过/未运行项）；
4. ZIP 不含会话/模型密钥和运行数据库（敏感文件排除，密钥值不进包）；
5. 版本/报告必须同 hash：导出时版本树哈希与版本行 content_hash 对账、
   打包报告与版本行冻结报告对账，不一致 → 拒绝导出（409）；
6. 导出接口：POST /api/projects/{pid}/skill-versions/{vid}/export 返回收据，
   未验证候选可导出但收据显示 candidate 而非验收通过。
"""
import json
import zipfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    DeliveryStatus,
    EngineeringProfile,
    Requirement,
    RequirementContract,
)
from app.generation.versioning import _tree_hash, _tree_manifest
from app.models import Project, SkillVersion

AUTH = {"x-session-token": "test-token"}

SKILL_MD = """# 测试技能

把会议纪要整理成行动项。
"""
EXTRACT_PY = "print('extract')\n"
SECRET_ENV = "SKILLFORGE_TOKEN=abc123-super-secret\n"


def _contract() -> RequirementContract:
    return RequirementContract.model_validate({
        "request_id": "flow-1",
        "target_platform": "codex",
        "requirements": [Requirement.model_validate({
            "requirement_id": "REQ-1",
            "content": "整理会议纪要为行动项",
            "necessary": True,
            "source_type": "user_message",
            "source_ref": "m1",
            "confirmation": "confirmed",
        }).model_dump()],
    })


def _profile() -> EngineeringProfile:
    return EngineeringProfile.model_validate({
        "profile_id": "default",
        "revision": 3,
        "rules": [{
            "rule_id": "SKILL_HEADER",
            "revision": 1,
            "severity": "blocking",
            "description": "SKILL.md 必须以一级标题开头",
            "checker_id": "validation:skill_header",
        }],
    })


def _checks() -> CheckReport:
    """静态过、执行过、覆盖 not_run（manual 待验收）→ 未验证候选。"""
    return CheckReport(checks=[
        CheckResult(check_id="validation:skill_header", status=CheckStatus.PASSED,
                    evidence="SKILL.md 以 # 开头"),
        CheckResult(check_id="runtime:python", status=CheckStatus.PASSED,
                    evidence="python 可用"),
        CheckResult(check_id="coverage:REQ-1", status=CheckStatus.NOT_RUN,
                    evidence="manual 待验收：需人工确认行动项格式"),
    ])


def _make_version_dir(base: Path, name: str, *, with_secrets: bool) -> tuple[str, Path]:
    """在 base/versions/ 下建版本目录（base 需为所属项目的 skills 目录）。"""
    from app.core.ulid import new_ulid

    vid = new_ulid()
    vdir = base / "versions" / vid
    (vdir / "scripts").mkdir(parents=True)
    (vdir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8", newline="\n")
    (vdir / "scripts" / "extract.py").write_text(EXTRACT_PY, encoding="utf-8", newline="\n")
    if with_secrets:
        # 混入敏感文件：不得进包（验收 4）
        (vdir / "local.db").write_text("fake sqlite db", encoding="utf-8")
        (vdir / "session.env").write_text(SECRET_ENV, encoding="utf-8")
    return vid, vdir


def test_missing_execution_and_host_evidence_cannot_be_delivered(tmp_path: Path) -> None:
    from app.generation.delivery import package_delivery

    vid, vdir = _make_version_dir(tmp_path, "missing-dimensions", with_secrets=False)
    checks = CheckReport(checks=[
        CheckResult(check_id="validation:skill_header", status=CheckStatus.PASSED),
        CheckResult(check_id="coverage:REQ-1", status=CheckStatus.PASSED),
    ])

    receipt = package_delivery("flow-1", vid, _contract(), checks, vdir)

    assert receipt.delivery_status == DeliveryStatus.CANDIDATE
    assert receipt.execution_status is None
    assert receipt.host_status is None
    assert not receipt.fully_verified
    assert any("execution" in note for note in receipt.notes)
    assert any("host" in note for note in receipt.notes)


def test_export_preserves_user_readme_and_delivery_tree(tmp_path: Path) -> None:
    from app.generation.delivery import package_delivery

    vid, vdir = _make_version_dir(tmp_path, "colliding-metadata", with_secrets=False)
    user_readme = "# User instructions\nKeep every workflow intact.\n"
    user_receipt = "user-owned payload\n"
    (vdir / "README.md").write_text(user_readme, encoding="utf-8")
    (vdir / "delivery").mkdir()
    (vdir / "delivery" / "receipt.json").write_text(user_receipt, encoding="utf-8")
    checks = CheckReport(checks=[
        CheckResult(check_id="validation:skill_header", status=CheckStatus.PASSED),
        CheckResult(check_id="runtime:python", status=CheckStatus.PASSED),
        CheckResult(check_id="coverage:REQ-1", status=CheckStatus.PASSED),
        CheckResult(check_id="host:codex:available", status=CheckStatus.PASSED),
    ])

    receipt = package_delivery("flow-1", vid, _contract(), checks, vdir)

    with zipfile.ZipFile(receipt.zip_path) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))
        assert zf.read("README.md").decode("utf-8") == user_readme
        assert zf.read("delivery/receipt.json").decode("utf-8") == user_receipt
        manifest = json.loads(zf.read("delivery-1/manifest.json"))
        metadata_root = manifest["metadata_root"]
        assert metadata_root != "delivery"
        assert "依赖" in zf.read(f"{metadata_root}/README.md").decode("utf-8")
        assert json.loads(zf.read(f"{metadata_root}/receipt.json"))["version_id"] == vid


def test_metadata_root_avoids_case_insensitive_path_collisions() -> None:
    from app.generation.delivery import _metadata_root

    assert _metadata_root([("DELIVERY/receipt.json", b"user")]) == "delivery-1"


def test_complete_zip_receipt_hash_and_unverified_label(
    migrated_db: Path, tmp_path: Path, client: TestClient,
) -> None:
    from app.generation.delivery import package_delivery

    contract = _contract()
    checks = _checks()
    profile = _profile()

    # ---------- 块 A：打包未验证候选 ----------
    vid, vdir = _make_version_dir(tmp_path, "unit", with_secrets=True)
    receipt = package_delivery(
        "flow-1", vid, contract, checks, vdir, profile=profile,
    )

    # 验收 3：not_run 存在 → 绝不"全通过"，如实标候选
    assert receipt.delivery_status == DeliveryStatus.CANDIDATE
    assert receipt.fully_verified is False
    assert receipt.static_status == CheckStatus.PASSED
    assert receipt.execution_status == CheckStatus.PASSED
    assert receipt.coverage_status == CheckStatus.NOT_RUN
    assert receipt.host_status is None
    assert any("not_run" in n or "未验证" in n for n in receipt.notes), receipt.notes
    assert any(c.check_id == "coverage:REQ-1" for c in receipt.requirement_coverage)

    # ---------- 块 B：ZIP 解包、必需文件、哈希一致 ----------
    zip_path = Path(receipt.zip_path)
    assert zip_path.is_file(), receipt.zip_path
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        # 验收 1：必需文件齐全（技能文件 + README + 交付物）
        required = {
            "SKILL.md", "scripts/extract.py", "README.md",
            "delivery/receipt.json", "delivery/manifest.json",
            "delivery/contract.json", "delivery/report.json",
            "delivery/profile.json",
        }
        assert required <= names, f"缺必需文件: {required - names}"

        # 验收 4：会话/密钥/数据库不进包
        assert not any(n.endswith(".db") for n in names), names
        assert not any(n.endswith(".env") for n in names), names
        blob = b"".join(zf.read(n) for n in names)
        assert b"super-secret" not in blob
        assert b"SKILLFORGE_TOKEN" not in blob

        # 验收 1：逐文件 sha256 与清单一致；技能文件字节与源一致
        manifest = json.loads(zf.read("delivery/manifest.json"))
        mfiles = {f["path"]: f["sha256"] for f in manifest["files"]}
        assert set(mfiles) == names - {"delivery/manifest.json"}
        for n in sorted(names):
            if n == "delivery/manifest.json":
                continue
            assert sha256(zf.read(n)).hexdigest() == mfiles[n], n
        assert zf.read("SKILL.md").decode("utf-8") == SKILL_MD
        assert zf.read("scripts/extract.py").decode("utf-8") == EXTRACT_PY

        # 版本/报告同 hash：manifest 记录的哈希可复核
        def _h(data: bytes) -> str:
            return sha256(data).hexdigest()

        assert manifest["version_id"] == vid
        assert manifest["flow_id"] == "flow-1"
        # 版本树哈希 = 非敏感文件（路径排序 + 文件哈希拼接再哈希）
        expect_tree = _h(
            f"SKILL.md:{_h(SKILL_MD.encode())}\n"
            f"scripts/extract.py:{_h(EXTRACT_PY.encode())}".encode()
        )
        assert manifest["version_hash"] == expect_tree
        # 报告哈希 = 包内报告字节哈希
        assert manifest["report_hash"] == _h(zf.read("delivery/report.json"))

        # 包内报告与传入报告同内容（模型无法在打包层伪造）
        packed_report = CheckReport.model_validate_json(zf.read("delivery/report.json"))
        assert packed_report == checks

        # 包内收据如实标注候选
        packed_receipt = json.loads(zf.read("delivery/receipt.json"))
        assert packed_receipt["delivery_status"] == "candidate"
        assert packed_receipt["version_id"] == vid

        # 验收 2：README 说明依赖/使用 + 未验证候选警示
        readme = zf.read("README.md").decode("utf-8")
        assert "依赖" in readme and "使用" in readme
        assert "Python" in readme  # scripts/extract.py → Python 依赖说明
        assert "candidate" in readme
        assert "未验证" in readme

        # 工程规则版本入包
        packed_profile = json.loads(zf.read("delivery/profile.json"))
        assert packed_profile["profile_id"] == "default"
        assert packed_profile["revision"] == 3

    # ---------- 块 C：导出接口（版本行 + 同 hash 对账） ----------
    cfg = load_config()
    pid = "01DELIVERY00000000000000000A"
    vid2, vdir2 = _make_version_dir(
        cfg.data_dir / "projects" / pid / "skills", "api", with_secrets=False,
    )
    files = _tree_manifest(vdir2)
    content_hash = _tree_hash(files)
    with Session(create_db_engine(db_path(cfg))) as s:
        if s.get(Project, pid) is None:
            s.add(Project(id=pid, name="delivery-export-test"))
        s.add(SkillVersion(
            id=vid2, project_id=pid, label="v1", content_hash=content_hash,
            files_json=json.dumps(files), warnings_json="[]",
            created_at=datetime.now(timezone.utc),
            generation_flow_id="flow-2",
            contract_json=contract.model_dump_json(),
            contract_hash=sha256(contract.model_dump_json().encode()).hexdigest(),
            profile_hash=sha256(profile.model_dump_json().encode()).hexdigest(),
            report_json=checks.model_dump_json(),
        ))
        s.commit()

    resp = client.post(
        f"/api/projects/{pid}/skill-versions/{vid2}/export", headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 验收 6：未验证候选可导出，但收据显示 candidate 而非验收通过
    assert body["delivery_status"] == "candidate"
    assert body["fully_verified"] is False if "fully_verified" in body else True
    # （fully_verified 是收据的派生性质，不序列化进 JSON；用五维状态如实判定）
    assert body["static_status"] == "passed"
    assert body["coverage_status"] == "not_run"
    # 审计 A1：导出时刻已执行宿主验证——无流程信息 → host 维度 not_run（非 None）
    assert body["host_status"] == "not_run"
    assert Path(body["zip_path"]).is_file()

    # 验收 5：版本/报告必须同 hash——篡改版本目录后导出被拒（409）
    (vdir2 / "SKILL.md").write_text("# 被篡改\n", encoding="utf-8")
    resp = client.post(
        f"/api/projects/{pid}/skill-versions/{vid2}/export", headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "version_hash_mismatch"
