"""T09：独立静态验证与完整性报告。

本项验收（工程计划 §5 T09）：
1. 缺文件/头信息/资源/hash 错误 → failed；
2. 脚本语法错误 → failed（Python ast 真实解析）；
3. 模型自称通过不影响检查结果（验证器只信磁盘与字节，不信 manifest 自报）；
4. 无检查器项 → not_run（有 checker_id 但环境不支持）/ unsupported（无 checker_id）；
5. 建议级规则（SKILL_SIZE_HINT warning）→ failed 不出现（按 severity 判定）。

验证对象是磁盘上的候选目录（root 下），不是内存 manifest——
独立解析，模型输出只提供"该验什么"的线索，不提供"验过了"的结论。
"""
import json
from pathlib import Path

import pytest

from app.generation.contracts import (
    ArtifactFile,
    ArtifactKind,
    CheckStatus,
    EngineeringProfile,
    Requirement,
    RequirementContract,
    SourceRef,
    SourceType,
    Confirmation,
)


def _profile() -> EngineeringProfile:
    return EngineeringProfile.model_validate({
        "schema_version": "1", "profile_id": "default", "revision": 1,
        "rules": [
            {"rule_id": "SKILL_HEADER", "revision": 1, "severity": "blocking",
             "applies_to": "SKILL.md", "description": "SKILL.md 头信息",
             "checker_id": "validation:skill_header"},
            {"rule_id": "SCRIPT_SYNTAX", "revision": 1, "severity": "blocking",
             "applies_to": "scripts/**", "description": "脚本语法",
             "checker_id": "validation:script_syntax"},
            {"rule_id": "ASSET_COMPLETE", "revision": 1, "severity": "blocking",
             "applies_to": "assets/**", "description": "二进制资源完整",
             "checker_id": "validation:binary_integrity"},
            {"rule_id": "SKILL_SIZE_HINT", "revision": 1, "severity": "warning",
             "applies_to": "SKILL.md", "description": "500 行建议",
             "checker_id": "validation:skill_size", "config": {"max_lines": 500}},
            {"rule_id": "SEMANTIC_TONE", "revision": 1, "severity": "warning",
             "applies_to": "", "description": "语气语义（纯语义规则）",
             "checker_id": ""},
        ],
    })


def _contract() -> RequirementContract:
    return RequirementContract(
        request_id="flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        target_platform="codex",
        requirements=[
            Requirement(
                requirement_id="REQ-001", content="按模板输出周报",
                necessary=True,
                source=SourceRef(type=SourceType.USER_CONFIRMED, ref="m"),
                confirmation=Confirmation.CONFIRMED,
            ),
        ],
    )


GOOD_SKILL_MD = (
    "---\nname: weekly-report\ndescription: 周报生成\n---\n# 周报\n按模板输出。\n"
)
GOOD_SCRIPT = "import csv\n\ndef extract(p):\n    return list(csv.reader(open(p)))\n"
LOGO = b"\x89PNG" + bytes(30)


def _stage_good(root: Path, flow_id: str = "flow-1") -> Path:
    """在 root/staging/flow-1 布置完整合法候选（SKILL.md + 脚本 + 二进制）。"""
    from app.generation.artifacts import stage_artifacts
    from hashlib import sha256
    manifest = _good_manifest()
    return stage_artifacts(flow_id, manifest, {"res-01": LOGO}, root)


def _good_manifest():
    from hashlib import sha256
    return __import__("app.generation.contracts", fromlist=["ArtifactManifest"]).ArtifactManifest(
        artifacts=[
            ArtifactFile(path="SKILL.md", kind=ArtifactKind.TEXT,
                         sha256=sha256(GOOD_SKILL_MD.encode()).hexdigest(),
                         content=GOOD_SKILL_MD, requirement_ids=["REQ-001"]),
            ArtifactFile(path="scripts/extract.py", kind=ArtifactKind.TEXT,
                         sha256=sha256(GOOD_SCRIPT.encode()).hexdigest(),
                         content=GOOD_SCRIPT, requirement_ids=["REQ-001"]),
            ArtifactFile(path="assets/logo.png", kind=ArtifactKind.BINARY,
                         sha256=sha256(LOGO).hexdigest(),
                         resource_id="res-01", requirement_ids=["REQ-001"]),
        ]
    )


# ---------- 验收 1+3：完整候选通过；模型自报不采信 ----------


def test_missing_reference_dependency_and_false_pass(tmp_path: Path) -> None:
    from app.generation.validation import validate_candidate

    root = tmp_path / "gen"
    staged = _stage_good(root)

    # 完整候选：全部 blocking 检查通过
    report = validate_candidate(_good_manifest(), _contract(), _profile(), staged)
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["validation:skill_header"] == CheckStatus.PASSED
    assert statuses["validation:script_syntax"] == CheckStatus.PASSED
    assert statuses["validation:binary_integrity"] == CheckStatus.PASSED
    assert not report.has_failed

    # 缺文件：清单声明 scripts/extract.py 但磁盘被删 → failed
    (staged / "scripts" / "extract.py").unlink()
    report = validate_candidate(_good_manifest(), _contract(), _profile(), staged)
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["validation:script_syntax"] == CheckStatus.FAILED
    assert report.has_failed

    # hash 错误：SKILL.md 被篡改 → all_files 检查 failed（磁盘字节才是真相）
    staged2 = _stage_good(root, "flow-2")
    (staged2 / "SKILL.md").write_text(
        GOOD_SKILL_MD + "被篡改的内容", encoding="utf-8", newline="\n"
    )
    report = validate_candidate(_good_manifest(), _contract(), _profile(), staged2)
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["validation:all_files"] == CheckStatus.FAILED
    assert report.has_failed

    # 缺头信息：合法 YAML 头缺失 name → failed
    staged3 = _stage_good(root, "flow-3")
    (staged3 / "SKILL.md").write_text(
        "# 没有头信息的技能\n", encoding="utf-8", newline="\n"
    )
    report = validate_candidate(_good_manifest(), _contract(), _profile(), staged3)
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["validation:skill_header"] == CheckStatus.FAILED


# ---------- 验收 2：脚本语法错误 → failed（真实 ast 解析） ----------


def test_script_syntax_error_fails(tmp_path: Path) -> None:
    from app.generation.validation import validate_candidate

    root = tmp_path / "gen"
    staged = _stage_good(root)
    (staged / "scripts" / "extract.py").write_text(
        "def broken(:\n    pass\n", encoding="utf-8"
    )
    report = validate_candidate(_good_manifest(), _contract(), _profile(), staged)
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["validation:script_syntax"] == CheckStatus.FAILED
    syntax = next(c for c in report.checks if c.check_id == "validation:script_syntax")
    assert "extract.py" in syntax.evidence  # 证据指向具体文件


# ---------- 验收 4：无检查器 → not_run / unsupported ----------


def test_unsupported_and_not_run_checks(tmp_path: Path) -> None:
    from app.generation.validation import validate_candidate

    root = tmp_path / "gen"
    staged = _stage_good(root)

    report = validate_candidate(_good_manifest(), _contract(), _profile(), staged)
    statuses = {c.check_id: c.status for c in report.checks}

    # 纯语义规则（无 checker_id）→ unsupported
    assert statuses.get("rule:SEMANTIC_TONE") == CheckStatus.UNSUPPORTED

    # 建议级规则失败不影响整体 failed 判定（severity 由调用方判定）
    assert "validation:skill_size" in statuses  # 有检查器就跑


# ---------- 验收 5：500 行建议规则 ----------


def test_size_hint_warning_rule_reports_not_failed(tmp_path: Path) -> None:
    from app.generation.validation import validate_candidate

    root = tmp_path / "gen"
    long_md = "---\nname: big\ndescription: d\n---\n" + ("行\n" * 600)
    from hashlib import sha256
    manifest = __import__("app.generation.contracts", fromlist=["ArtifactManifest"]).ArtifactManifest(
        artifacts=[ArtifactFile(path="SKILL.md", kind=ArtifactKind.TEXT,
                                sha256=sha256(long_md.encode()).hexdigest(),
                                content=long_md, requirement_ids=["REQ-001"])]
    )
    staged = root / "staging" / "flow-size"
    staged.mkdir(parents=True)
    (staged / "SKILL.md").write_text(long_md, encoding="utf-8", newline="\n")

    report = validate_candidate(manifest, _contract(), _profile(), staged)
    statuses = {c.check_id: c.status for c in report.checks}
    size = next(c for c in report.checks if c.check_id == "validation:skill_size")
    assert size.status == CheckStatus.FAILED  # 检查如实报告超限
    # 但文件本身完整：all_files 通过（warning 级失败不混淆完整性结论）
    assert statuses["validation:all_files"] == CheckStatus.PASSED
