"""备份导出/恢复（S7 阶段封存，细化方案 §8 S7 行）。

语义：
- 备份 = 数据目录完整快照（zip）。数据库经 SQLite backup API 取一致快照
  （WAL 下直接复制文件不安全）；其余文件（projects/ 等）原样归档。
- 恢复 = 校验清单后整体还原到**空**数据目录（拒绝覆盖既有数据）。
- zip 路径全部校验（防 zip-slip 逃逸）。
- 恢复后可用 `alembic upgrade head` 把旧备份迁移到当前 schema
  （alembic_version 随快照一起保留）。

备份/恢复应在服务停止时执行（CLI 会提示）。
"""
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import AppConfig

TOOL_NAME = "skill-forge"
MANIFEST_VERSION = 1
DB_NAME = "app.db"
# 备份时跳过的数据库伴生文件（WAL/SHM，快照已含其内容）
SKIP_NAMES = {DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"}
MANIFEST_NAME = "manifest.json"


class BackupError(Exception):
    """备份/恢复失败（结构化错误，CLI 转 stderr 退出码 1）。"""


class MissingDatabaseError(BackupError):
    """数据目录中没有数据库，无可备份内容。"""


class InvalidBackupError(BackupError):
    """备份包缺清单或结构非法。"""


class TargetNotEmptyError(BackupError):
    """恢复目标目录非空，拒绝覆盖（主§14：不静默销毁数据）。"""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _schema_version(db_file: Path) -> str:
    """读取快照库的 alembic 版本；无版本表（空库）返回 "none"。"""
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else "none"
    except sqlite3.OperationalError:
        return "none"
    finally:
        con.close()


def _safe_arcname(name: str) -> bool:
    """zip 成员名必须是相对路径且不逃逸（zip-slip 防护）。"""
    if not name or name.startswith(("/", "\\")) or ":" in name:
        return False
    parts = Path(name).parts
    return ".." not in parts and not name.endswith(("/", "\\"))


def create_backup(cfg: AppConfig, out_path: Path) -> dict:
    """导出数据目录到 zip；返回写入包内的 manifest。"""
    db_file = cfg.data_dir / DB_NAME
    if not db_file.exists():
        raise MissingDatabaseError(f"数据库不存在，无可备份内容: {db_file}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0

    with tempfile.TemporaryDirectory(prefix="skillforge-backup-") as tmp:
        # 1) SQLite 一致快照（backup API，兼容 WAL）
        snap = Path(tmp) / DB_NAME
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        schema_version = _schema_version(snap)

        manifest = {
            "tool": TOOL_NAME,
            "manifest_version": MANIFEST_VERSION,
            "created_at": _utcnow_iso(),
            "schema_version": schema_version,
            "db_file": DB_NAME,
        }

        # 2) 归档：manifest + db 快照 + 其余数据文件
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.write(snap, DB_NAME)
            file_count += 1
            for path in sorted(cfg.data_dir.rglob("*")):
                if not path.is_file() or path.name in SKIP_NAMES:
                    continue
                rel = path.relative_to(cfg.data_dir).as_posix()
                if not _safe_arcname(rel):
                    continue  # 数据目录内不应出现，防御性跳过
                zf.write(path, rel)
                file_count += 1

    manifest["file_count"] = file_count
    return manifest


def read_manifest(zip_path: Path) -> dict:
    """读备份清单；缺清单或非 skill-forge 包则拒绝。"""
    if not zipfile.is_zipfile(zip_path):
        raise InvalidBackupError(f"不是 zip 备份包: {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        if MANIFEST_NAME not in zf.namelist():
            raise InvalidBackupError(f"备份包缺少 {MANIFEST_NAME}，不是 skill-forge 备份")
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    if manifest.get("tool") != TOOL_NAME:
        raise InvalidBackupError(f"备份清单 tool 不匹配: {manifest.get('tool')!r}")
    if int(manifest.get("manifest_version", 0)) > MANIFEST_VERSION:
        raise InvalidBackupError(
            f"备份清单版本过新（{manifest.get('manifest_version')}），请升级本工具后再恢复"
        )
    return manifest


def restore_backup(zip_path: Path, data_dir: Path) -> dict:
    """恢复备份到空数据目录；返回 manifest。

    目标目录必须不存在或为空——绝不静默覆盖既有数据。
    """
    manifest = read_manifest(zip_path)

    if data_dir.exists() and any(data_dir.iterdir()):
        raise TargetNotEmptyError(
            f"恢复目标非空（{data_dir}），拒绝覆盖。请先清空或换目录。"
        )

    db_name = manifest.get("db_file", DB_NAME)
    with tempfile.TemporaryDirectory(prefix="skillforge-restore-") as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir() or not _safe_arcname(info.filename):
                    raise InvalidBackupError(f"备份包内含非法路径: {info.filename!r}")
            zf.extractall(tmp)

        tmp_root = Path(tmp)
        if not (tmp_root / db_name).exists():
            raise InvalidBackupError(f"备份包缺少数据库文件 {db_name}")

        # 完整性校验：快照库可打开且通过 quick_check
        con = sqlite3.connect(tmp_root / db_name)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise InvalidBackupError(f"数据库完整性校验失败: {row[0] if row else '无结果'}")
        finally:
            con.close()

        data_dir.mkdir(parents=True, exist_ok=True)
        for entry in tmp_root.iterdir():
            shutil.move(str(entry), str(data_dir / entry.name))

    return manifest


def backup_info(zip_path: Path) -> dict:
    """只读查看备份信息（含文件清单）。"""
    manifest = read_manifest(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        manifest["entries"] = zf.namelist()
    return manifest
