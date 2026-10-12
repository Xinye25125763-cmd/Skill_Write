"""v1.3 砍后清单：三个真实价值检查器（TDD）。

- validation:no_secrets —— 复用打包侧扫描逻辑（v1.3 评估砍后保留项）；
- validation:file_hash —— 全部文件磁盘实测哈希 vs manifest；
- execution:script —— 脚本空输入 smoke（崩溃/超时如实 FAILED）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.generation.contracts import (
    ArtifactFile,
    ArtifactKind,
    ArtifactManifest,
    CheckStatus,
    EngineeringProfile,
    RequirementContract,
    SkillPlan,
)
from app.generation.validation import _CHECKER_MAP, validate_candidate


def _contract() -> RequirementContract:
    return RequirementContract.model_validate({
        "request_id": "flow-1", "target_platform": "catpaw",
        "requirements": [{
            "requirement_id": "REQ-1", "content": "整理行动项", "necessary": True,
            "source": {"type": "user_message", "ref": "m1"},
            "confirmation": "confirmed",
        }],
    })


def _plan() -> SkillPlan:
    return SkillPlan.model_validate({
        "plan_id": "p1",
        "files": [{"path": "SKILL.md", "kind": "text", "purpose": "主文档",
                    "requirement_ids": ["REQ-1"]}],
        "acceptances": [{"acceptance_id": "A1", "kind": "static",
                         "description": "SKILL.md 存在", "requirement_ids": ["REQ-1"]}],
    })


def _profile(checker_ids: list[str]) -> EngineeringProfile:
    return EngineeringProfile.model_validate({
        "profile_id": "test", "revision": 1,
        "rules": [
            {"rule_id": f"RULE{i}", "revision": 1, "severity": "blocking",
             "description": f"规则{i}", "checker_id": cid}
            for i, cid in enumerate(checker_ids)
        ],
    })


def _manifest_of(files: dict[str, bytes], root: Path) -> ArtifactManifest:
    arts = []
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        import hashlib
        arts.append(ArtifactFile(
            path=rel.replace("\\", "/"),
            kind=ArtifactKind.TEXT,
            requirement_ids=["REQ-1"],
            sha256=hashlib.sha256(data).hexdigest(),
            content=data.decode("utf-8", errors="replace"),
        ))
    return ArtifactManifest(artifacts=arts)


def test_no_secrets_file_hash_and_execution_checkers(tmp_path: Path) -> None:
    root = tmp_path / "cand"

    # ---------- 块 A：no_secrets ----------
    clean = {"SKILL.md": b"---\nname: t\n---\n# ok\n", "notes.md": b"normal text"}
    m = _manifest_of(clean, root)
    report = validate_candidate(m, _contract(), _profile(["validation:no_secrets"]), root)
    r = next(c for c in report.checks if c.check_id == "validation:no_secrets")
    assert r.status == CheckStatus.PASSED, r.evidence

    dirty = dict(clean)
    dirty["notes.md"] = b"config with API_KEY=sk-12345 inside"
    root2 = tmp_path / "dirty"
    m2 = _manifest_of(dirty, root2)
    report2 = validate_candidate(m2, _contract(), _profile(["validation:no_secrets"]), root2)
    r2 = next(c for c in report2.checks if c.check_id == "validation:no_secrets")
    assert r2.status == CheckStatus.FAILED, r2.evidence
    assert "notes.md" in r2.evidence

    # ---------- 块 B：file_hash ----------
    root3 = tmp_path / "hashed"
    files = {"SKILL.md": b"---\nname: t\n---\n# x\n", "scripts/run.py": b"print(1)\n"}
    m3 = _manifest_of(files, root3)
    report3 = validate_candidate(m3, _contract(), _profile(["validation:file_hash"]), root3)
    r3 = next(c for c in report3.checks if c.check_id == "validation:file_hash")
    assert r3.status == CheckStatus.PASSED, r3.evidence

    # 篡改磁盘 → FAILED
    (root3 / "scripts" / "run.py").write_bytes(b"print('tampered')\n")
    report4 = validate_candidate(m3, _contract(), _profile(["validation:file_hash"]), root3)
    r4 = next(c for c in report4.checks if c.check_id == "validation:file_hash")
    assert r4.status == CheckStatus.FAILED
    assert "scripts/run.py" in r4.evidence

    # ---------- 块 C：execution:script 空输入 smoke ----------
    root5 = tmp_path / "exec"
    good = {
        "SKILL.md": b"---\nname: t\n---\n# x\n",
        "scripts/ok.py": b"import sys\nprint('ok', file=sys.stderr)\n",
    }
    m5 = _manifest_of(good, root5)
    report5 = validate_candidate(m5, _contract(), _profile(["execution:script"]), root5)
    r5 = next(c for c in report5.checks if c.check_id == "execution:script")
    assert r5.status == CheckStatus.PASSED, r5.evidence
    assert "ok.py" in r5.evidence

    # 崩溃脚本 → FAILED，文件名进证据
    bad = {
        "SKILL.md": b"---\nname: t\n---\n# x\n",
        "scripts/crash.py": b"raise SystemExit('boom')\n",
        "scripts/ok.py": b"print('fine')\n",
    }
    root6 = tmp_path / "exec_bad"
    m6 = _manifest_of(bad, root6)
    report6 = validate_candidate(m6, _contract(), _profile(["execution:script"]), root6)
    r6 = next(c for c in report6.checks if c.check_id == "execution:script")
    assert r6.status == CheckStatus.FAILED, r6.evidence
    assert "crash.py" in r6.evidence

    # 无脚本 → PASSED（"无脚本可执行"）
    root7 = tmp_path / "no_scripts"
    m7 = _manifest_of({"SKILL.md": b"---\nname: t\n---\n# x\n"}, root7)
    report7 = validate_candidate(m7, _contract(), _profile(["execution:script"]), root7)
    r7 = next(c for c in report7.checks if c.check_id == "execution:script")
    assert r7.status == CheckStatus.PASSED

    # ---------- 块 D：注册进 _CHECKER_MAP（profile 可直接触发） ----------
    for cid in ("validation:no_secrets", "validation:file_hash", "execution:script"):
        assert cid in _CHECKER_MAP, f"{cid} 未注册"
