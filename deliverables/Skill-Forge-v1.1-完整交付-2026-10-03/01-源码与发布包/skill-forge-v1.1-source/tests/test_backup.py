"""S7 备份导出/恢复测试。

完成定义（细化方案 §8 S7 行）：备份后清空数据目录可恢复项目。
"""
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.core.ulid import new_ulid
from app.models import BriefRevision, Project, Suite
from app.services.backup import (
    InvalidBackupError,
    MissingDatabaseError,
    TargetNotEmptyError,
    backup_info,
    create_backup,
    read_manifest,
    restore_backup,
)
from app.services.skill_files import SkillFileService

AUTH = {"x-session-token": "test-token"}

_engines_under_test: list = []


def _engine(cfg):
    """建引擎并登记，便于清空数据目录前统一 dispose 释放句柄。"""
    from app.core.db import create_db_engine as _cde

    eng = _cde(db_path(cfg))
    _engines_under_test.append(eng)
    return eng


def _release_all_engines() -> None:
    """释放测试与应用路由层缓存的全部数据库句柄（Windows 删除文件必需）。"""
    from app.api import projects as _projects_api

    for eng in _engines_under_test:
        eng.dispose()
    _engines_under_test.clear()
    for eng in _projects_api._engines.values():
        eng.dispose()
    _projects_api._engines.clear()


def _make_project_data(client: TestClient, cfg) -> tuple[str, str, str, str]:
    """造一套代表性数据：项目 + brief + 技能版本 + 测试集，返回关键 ID。"""
    resp = client.post("/api/projects", json={"name": "备份项目"}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    # brief 修订
    brief = {
        "purpose": {"value": "整理纪要", "source": {"type": "user_message", "ref": "m1"}},
        "trigger_conditions": [
            {"value": "整理纪要", "source": {"type": "user_message", "ref": "m1"}}
        ],
        "non_trigger_conditions": [
            {"value": "写通知", "source": {"type": "system_inferred", "ref": ""}}
        ],
        "inputs": [{"name": "纪要", "format": "text", "required": True, "validation": "manual"}],
        "outputs": [{"name": "列表", "format": "markdown", "required": True, "validation": "manual"}],
        "tools": [],
        "constraints": [{"value": "中文", "source": {"type": "user_message", "ref": "m2"}}],
        "success_criteria": [
            {"value": "含负责人", "source": {"type": "user_confirmed", "ref": ""}}
        ],
        "target_platform": "catpaw",
        "evaluation_preference": {"objective_tests": True, "subjective_scale": False},
    }
    r = client.post(f"/api/projects/{pid}/briefs", json={"brief": brief}, headers=AUTH)
    assert r.status_code == 201, r.text
    brief_id = r.json()["revision_id"]

    # 技能草稿 + 版本
    skill_md = f"---\nname: backup-skill\ndescription: 备份验证\n---\n\n正文。\n"
    r = client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": skill_md},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "bk"}, headers=AUTH)
    assert r.status_code == 202, r.text
    vid = r.json()["job_id"]  # 仅记录，版本由 Worker 创建；测试夹具里用直接落库替代

    # 直接走服务层造不可变版本（不走 Worker，备份测试不需要执行链路）
    with Session(_engine(cfg)) as s:
        import hashlib

        svc = SkillFileService(cfg, s)
        from app.models import SkillVersion
        from app.services.skill_files import _tree_hash

        file_hash = hashlib.sha256(skill_md.encode("utf-8")).hexdigest()
        files = [{"path": "SKILL.md", "hash": file_hash, "size": len(skill_md.encode("utf-8")), "kind": "text"}]

        version = SkillVersion(
            id=new_ulid(),
            project_id=pid,
            label="bk",
            content_hash=_tree_hash(files),
            files_json=json.dumps(files, ensure_ascii=False),
            brief_revision_id=brief_id,
        )
        s.add(version)
        s.commit()
        version_id = version.id
        version_root = cfg.data_dir / "projects" / pid / "skills" / "versions" / version_id
        version_root.mkdir(parents=True, exist_ok=True)
        (version_root / "SKILL.md").write_text(skill_md, encoding="utf-8", newline="\n")

        # 测试集
        suite = Suite(
            id=new_ulid(),
            project_id=pid,
            name="备份套件",
            cases_json=json.dumps(
                [{"id": "c1", "prompt": "p", "assertions": [{"type": "exit_reason_is", "value": "completed"}]}],
                ensure_ascii=False,
            ),
            created_at=0.0,
        )
        s.add(suite)
        s.commit()
        suite_id = suite.id

    return pid, brief_id, version_id, suite_id


def test_backup_then_wipe_then_restore(migrated_db: Path, client: TestClient, tmp_path: Path) -> None:
    """完成定义：备份 → 清空数据目录 → 恢复 → 项目与关键数据完整可访问。"""
    cfg = load_config()
    pid, brief_id, version_id, suite_id = _make_project_data(client, cfg)

    # 备份前额外写入一个数据文件（验证 projects/ 下文件也被归档）
    extra = cfg.data_dir / "projects" / pid / "extra.txt"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("额外数据", encoding="utf-8")

    # 1) 备份
    backup_path = tmp_path / "backup.zip"
    manifest = create_backup(cfg, backup_path)
    assert backup_path.exists()
    assert manifest["tool"] == "skill-forge"
    assert manifest["schema_version"] != "none"  # alembic 版本被记录
    assert manifest["file_count"] >= 1

    with zipfile.ZipFile(backup_path) as zf:
        names = zf.namelist()
    assert "app.db" in names
    assert f"projects/{pid}/extra.txt" in names  # 数据文件被归档
    assert "manifest.json" in names

    # 2) 记录恢复前后用于比对的库内事实
    with Session(_engine(cfg)) as s:
        before_projects = sorted(
            (p.id, p.name) for p in s.query(Project).all()
        )
        before_briefs = {b.id: b.brief_json for b in s.query(BriefRevision).all()}
        before_suites = {x.id: x.cases_json for x in s.query(Suite).all()}
    skill_md_before = (cfg.data_dir / "projects" / pid / "skills" / "versions" / version_id / "SKILL.md").read_text(encoding="utf-8")

    # 3) 清空数据目录（模拟灾难：整个 data 目录没了）
    _release_all_engines()  # 先释放全部 SQLite 句柄（备份/恢复也应停服执行，同理）
    for entry in cfg.data_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    assert not db_path(cfg).exists()
    assert not (cfg.data_dir / "projects").exists()

    # 4) 恢复
    restored_manifest = restore_backup(backup_path, cfg.data_dir)
    assert restored_manifest["schema_version"] == manifest["schema_version"]

    # 5) 验证：库内数据一致
    with Session(_engine(cfg)) as s:
        after_projects = sorted((p.id, p.name) for p in s.query(Project).all())
        after_briefs = {b.id: b.brief_json for b in s.query(BriefRevision).all()}
        after_suites = {x.id: x.cases_json for x in s.query(Suite).all()}
        svc = SkillFileService(cfg, s)
        version = svc.get_version(pid, version_id)
        assert version is not None
        assert svc.read_version_file(pid, version_id, "SKILL.md") == skill_md_before

    assert after_projects == before_projects
    assert after_briefs == before_briefs
    assert after_suites == before_suites

    # 6) 恢复后 API 可正常服务（新客户端指向恢复后的库）
    assert (cfg.data_dir / "projects" / pid / "extra.txt").read_text(encoding="utf-8") == "额外数据"
    r = client.get(f"/api/projects/{pid}", headers=AUTH)
    assert r.status_code == 200 and r.json()["name"] == "备份项目"
    r = client.get(f"/api/projects/{pid}/briefs/{brief_id}", headers=AUTH)
    assert r.status_code == 200
    r = client.get(f"/api/suites/{suite_id}", headers=AUTH)
    assert r.status_code == 200


def test_backup_missing_db_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空数据目录（无数据库）备份 → 结构化报错。"""
    from app.core.config import AppConfig

    empty = tmp_path / "empty-data"
    empty.mkdir()
    cfg = AppConfig(data_dir=empty)
    with pytest.raises(MissingDatabaseError):
        create_backup(cfg, tmp_path / "b.zip")


def test_restore_rejects_non_empty_target(migrated_db: Path, tmp_path: Path) -> None:
    """恢复目标非空 → 拒绝覆盖（主§14：不静默销毁数据）。"""
    cfg = load_config()
    (cfg.data_dir / "app.db").exists() or pytest.fail("夹具应已建库")
    backup_path = tmp_path / "b.zip"
    create_backup(cfg, backup_path)

    # 目标目录有一个文件 → 拒绝
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("既有数据", encoding="utf-8")
    with pytest.raises(TargetNotEmptyError):
        restore_backup(backup_path, target)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "既有数据"  # 原文件未动


def test_restore_rejects_bad_archives(tmp_path: Path) -> None:
    """非 zip / 无清单 / tool 不匹配 → 拒绝恢复。"""
    # 非 zip
    not_zip = tmp_path / "not.zip"
    not_zip.write_text("hello", encoding="utf-8")
    with pytest.raises(InvalidBackupError):
        read_manifest(not_zip)

    # zip 但无 manifest
    import zipfile as zf_mod

    no_manifest = tmp_path / "no-manifest.zip"
    with zf_mod.ZipFile(no_manifest, "w") as zf:
        zf.writestr("app.db", "junk")
    with pytest.raises(InvalidBackupError):
        read_manifest(no_manifest)

    # manifest tool 不匹配
    wrong_tool = tmp_path / "wrong-tool.zip"
    with zf_mod.ZipFile(wrong_tool, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"tool": "other", "manifest_version": 1}))
    with pytest.raises(InvalidBackupError):
        read_manifest(wrong_tool)

    # 清单版本过新
    future = tmp_path / "future.zip"
    with zf_mod.ZipFile(future, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"tool": "skill-forge", "manifest_version": 999}))
    with pytest.raises(InvalidBackupError):
        read_manifest(future)


def test_restore_rejects_zip_slip(migrated_db: Path, tmp_path: Path) -> None:
    """备份包内含逃逸路径（../evil）→ 拒绝恢复，不写任何文件。"""
    cfg = load_config()
    backup_path = tmp_path / "b.zip"
    create_backup(cfg, backup_path)

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"tool": "skill-forge", "manifest_version": 1}))
        zf.writestr("app.db", "junk")
        zf.writestr("../evil.txt", "escape")
    target = tmp_path / "restore-target"
    with pytest.raises(InvalidBackupError):
        restore_backup(evil, target)
    assert not (tmp_path / "evil.txt").exists()  # 未逃逸
    assert not target.exists() or not any(target.iterdir())  # 未落任何文件


def test_backup_info_roundtrip(migrated_db: Path, tmp_path: Path) -> None:
    """info 只读查看：清单 + 条目列表。"""
    cfg = load_config()
    backup_path = tmp_path / "b.zip"
    create_backup(cfg, backup_path)
    info = backup_info(backup_path)
    assert info["tool"] == "skill-forge"
    assert "app.db" in info["entries"]
    assert "manifest.json" in info["entries"]


def test_backup_cli(migrated_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI create/info/restore 退出码与输出。"""
    from app.backup import main as cli_main

    cfg = load_config()
    out = tmp_path / "cli-backup.zip"
    assert cli_main(["create", "--out", str(out)]) == 0
    assert "备份完成" in capsys.readouterr().out

    assert cli_main(["info", str(out)]) == 0
    assert "skill-forge" in capsys.readouterr().out

    # 恢复到空的独立目录
    target = tmp_path / "cli-restore"
    assert cli_main(["restore", str(out), "--data-dir", str(target)]) == 0
    assert (target / "app.db").exists()

    # 恢复到非空目录 → 退出码 1
    bad_target = tmp_path / "cli-bad"
    bad_target.mkdir()
    (bad_target / "x.txt").write_text("占位", encoding="utf-8")
    assert cli_main(["restore", str(out), "--data-dir", str(bad_target)]) == 1
