"""技能文件服务：草稿区（可变）与版本区（不可变快照）+ 四重安全校验。

安全校验（主§6.2 落盘前检查，细化方案 §6.2）：
    1. 路径穿越：拒绝绝对路径、.. 段、反斜杠、控制字符；
    2. 类型白名单：按扩展名；
    3. 体积与数量上限；
    4. 引用有效性：SKILL.md 必需、frontmatter 必含 name/description、
       正文本地文件链接必须存在。

设计纪律：
    - 草稿区是唯一可变区域；版本目录创建后只读；
    - 哈希为规范化树哈希：路径排序 → 每文件 sha256 → 拼接再 sha256；
    - 版本创建冲突（同项目同 content_hash）返回冲突，不静默复用。
"""
import hashlib
import json
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath

import yaml
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.ulid import new_ulid
from app.models import SkillVersion, utcnow

MAX_FILE_BYTES = 1_000_000       # 单文件 1MB
MAX_TOTAL_BYTES = 5_000_000      # 全树 5MB
MAX_FILES = 200                  # 文件数上限

ALLOWED_EXTS = frozenset({
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".css", ".html",
    ".csv", ".sh", ".ps1", ".ini", ".cfg",
})

MARKDOWN_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


class FileValidationError(Exception):
    """草稿/版本校验失败。errors 阻断，warnings 提示（不阻断）。"""

    def __init__(self, errors: list[str], warnings: list[str] | None = None) -> None:
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("; ".join(errors))


def normalize_relpath(raw: str) -> PurePosixPath:
    """规范化相对路径并做穿越检查，非法即抛 FileValidationError。

    规则：POSIX 风格正斜杠；拒绝绝对路径、.. 段、盘符、反斜杠、
    控制字符与空白路径；NFC 规范化避免同形路径分裂。
    """
    if not raw or not raw.strip():
        raise FileValidationError(["路径为空"])

    # 控制字符检查
    if any(ord(ch) < 0x20 for ch in raw):
        raise FileValidationError([f"路径含控制字符: {raw!r}"])

    if "\\" in raw:
        raise FileValidationError([f"路径含反斜杠（统一使用正斜杠）: {raw!r}"])

    raw = unicodedata.normalize("NFC", raw)
    p = PurePosixPath(raw)
    if p.is_absolute() or raw.startswith("/"):
        raise FileValidationError([f"拒绝绝对路径: {raw!r}"])

    # Windows 盘符形如 C:/x
    if re.match(r"^[A-Za-z]:", raw):
        raise FileValidationError([f"拒绝盘符路径: {raw!r}"])

    parts = p.parts
    if any(part == ".." for part in parts):
        raise FileValidationError([f"拒绝路径穿越: {raw!r}"])
    if any(part in (".", "") for part in parts):
        raise FileValidationError([f"拒绝特殊路径段: {raw!r}"])

    if p.suffix and p.suffix.lower() not in ALLOWED_EXTS:
        raise FileValidationError([f"不允许的文件类型: {p.suffix}（路径 {raw!r}）"])
    return p


class SkillFileService:
    """草稿与版本的文件操作。"""

    def __init__(self, cfg: AppConfig, session: Session) -> None:
        self.cfg = cfg
        self.session = session

    # ---------- 路径解析 ----------

    def _project_skills_root(self, project_id: str) -> Path:
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", project_id):
            raise FileValidationError([f"非法项目 ID: {project_id!r}"])
        return self.cfg.data_dir / "projects" / project_id / "skills"

    def draft_root(self, project_id: str) -> Path:
        return self._project_skills_root(project_id) / "drafts"

    def version_root(self, project_id: str, version_id: str) -> Path:
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", version_id):
            raise FileValidationError([f"非法版本 ID: {version_id!r}"])
        return self._project_skills_root(project_id) / "versions" / version_id

    def _resolve(self, root: Path, rel: str) -> tuple[PurePosixPath, Path]:
        """校验相对路径并解析为 root 下的绝对路径（双保险再 verify 一次）。"""
        relpath = normalize_relpath(rel)
        target = root.joinpath(*relpath.parts)
        # F05：is_relative_to 做层级判断，避免字符串前缀把兄弟目录误判为根内
        if not target.resolve().is_relative_to(root.resolve()):
            raise FileValidationError([f"路径越出根目录: {rel!r}"])
        return relpath, target

    # ---------- 草稿操作 ----------

    def list_draft(self, project_id: str) -> list[dict]:
        """草稿文件树：[{path, size}]，路径排序。"""
        root = self.draft_root(project_id)
        if not root.exists():
            return []
        out = []
        for f in sorted(root.rglob("*")):
            if f.is_file():
                out.append({"path": f.relative_to(root).as_posix(), "size": f.stat().st_size})
        return out

    def read_draft(self, project_id: str, rel: str) -> str:
        _, target = self._resolve(self.draft_root(project_id), rel)
        if not target.is_file():
            raise FileValidationError([f"文件不存在: {rel!r}"])
        return target.read_text(encoding="utf-8")

    def write_draft(self, project_id: str, rel: str, content: str) -> dict:
        """写草稿文件（自动保存语义）：全部校验通过才落盘。"""
        _, target = self._resolve(self.draft_root(project_id), rel)
        data = content.encode("utf-8")  # 编码校验：非 UTF-8 内容在此自然失败
        if len(data) > MAX_FILE_BYTES:
            raise FileValidationError([f"文件超过上限 {MAX_FILE_BYTES} 字节: {rel!r}"])

        existing = self.list_draft(project_id)
        if not target.exists() and len(existing) >= MAX_FILES:
            raise FileValidationError([f"文件数超过上限 {MAX_FILES}"])

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": rel, "size": len(data)}

    def delete_draft(self, project_id: str, rel: str) -> None:
        _, target = self._resolve(self.draft_root(project_id), rel)
        if target.is_file():
            target.unlink()
            # 清理空目录
            parent = target.parent
            while parent != self.draft_root(project_id) and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    # ---------- 树校验与哈希 ----------

    def validate_tree(self, root: Path) -> tuple[list[dict], str, list[str], list[str]]:
        """校验整棵文件树，返回 (清单, content_hash, errors, warnings)。

        引用有效性：SKILL.md 必需；frontmatter 的 name/description 必填；
        正文引用的本地文件必须存在。建议级问题（kebab-case、description
        长度、正文行数）以 warnings 返回，不阻断（主§6.2 建议与硬限分离）。
        """
        errors: list[str] = []
        warnings: list[str] = []

        files: list[dict] = []
        total = 0
        if root.exists():
            for f in sorted(root.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(root).as_posix()
                size = f.stat().st_size
                total += size
                if size > MAX_FILE_BYTES:
                    errors.append(f"文件超限: {rel}")
                digest = hashlib.sha256(f.read_bytes()).hexdigest()
                files.append({"path": rel, "hash": digest, "size": size})

        if not files:
            errors.append("空技能：至少需要 SKILL.md")
        if len(files) > MAX_FILES:
            errors.append(f"文件数超限: {len(files)} > {MAX_FILES}")
        if total > MAX_TOTAL_BYTES:
            errors.append(f"总体积超限: {total} > {MAX_TOTAL_BYTES}")

        paths = {f["path"] for f in files}

        if "SKILL.md" not in paths and files:
            errors.append("缺少 SKILL.md")

        skill_md_path = root / "SKILL.md"
        if "SKILL.md" in paths and skill_md_path.is_file():
            text = skill_md_path.read_text(encoding="utf-8")
            meta, body = _split_frontmatter(text)
            if meta is None:
                errors.append("SKILL.md frontmatter 解析失败（需 --- 包围的 YAML 头）")
            else:
                name = str(meta.get("name", "")).strip()
                desc = str(meta.get("description", "")).strip()
                if not name:
                    errors.append("frontmatter 缺少 name")
                else:
                    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
                        warnings.append(f"name 非 kebab-case: {name!r}")
                    if len(name) > 64:
                        warnings.append(f"name 超过 64 字符: {len(name)}")
                if not desc:
                    errors.append("frontmatter 缺少 description")
                elif len(desc) > 1024:
                    warnings.append(f"description 超过 1024 字符: {len(desc)}")
                if body is not None and body.count("\n") + 1 > 500:
                    warnings.append(f"SKILL.md 正文超过 500 行建议上限: {body.count(chr(10)) + 1}")

                # 引用有效性：正文本地文件链接必须存在
                if body:
                    for match in MARKDOWN_LINK_RE.finditer(body):
                        ref = match.group(1)
                        if ref.startswith(("http://", "https://", "#", "mailto:")):
                            continue
                        ref_clean = ref.split("#")[0]
                        if ref_clean and ref_clean not in paths:
                            errors.append(f"SKILL.md 引用的文件不存在: {ref_clean}")

        content_hash = _tree_hash(files)
        return files, content_hash, errors, warnings

    # ---------- 版本创建 ----------

    def create_version(
        self,
        project_id: str,
        parent_version_id: str | None = None,
        brief_revision_id: str | None = None,
        label: str | None = None,
    ) -> SkillVersion:
        """从草稿创建不可变版本：校验 → 冲突检查 → 复制 → 落库。

        复制采用"临时目录 → 校验 → 原子重命名"三段式（主§9 文件提交纪律）。
        """
        draft = self.draft_root(project_id)
        files, content_hash, errors, warnings = self.validate_tree(draft)
        if errors:
            raise FileValidationError(errors, warnings)

        conflict = (
            self.session.query(SkillVersion)
            .filter_by(project_id=project_id, content_hash=content_hash)
            .one_or_none()
        )
        if conflict is not None:
            raise VersionConflictError(conflict.id)

        version_id = new_ulid()
        final_dir = self.version_root(project_id, version_id)
        tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.parent.mkdir(parents=True, exist_ok=True)

        shutil.copytree(draft, tmp_dir)

        # 复制后重新校验，防复制竞态
        copied_files, copied_hash, copied_errors, _ = self.validate_tree(tmp_dir)
        if copied_errors or copied_hash != content_hash:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise FileValidationError(copied_errors or ["复制后哈希不一致，创建中止"])

        tmp_dir.rename(final_dir)  # 同盘原子重命名

        version = SkillVersion(
            id=version_id,
            project_id=project_id,
            parent_version_id=parent_version_id,
            brief_revision_id=brief_revision_id,
            label=label,
            content_hash=content_hash,
            files_json=json.dumps(files, ensure_ascii=False),
            warnings_json=json.dumps(warnings, ensure_ascii=False),
            status="validated",
            created_at=utcnow(),
        )
        self.session.add(version)
        self.session.commit()
        return version

    # ---------- 版本读取 ----------

    def list_versions(self, project_id: str) -> list[SkillVersion]:
        return (
            self.session.query(SkillVersion)
            .filter_by(project_id=project_id)
            .order_by(SkillVersion.created_at.desc())
            .all()
        )

    def get_version(self, project_id: str, version_id: str) -> SkillVersion | None:
        return (
            self.session.query(SkillVersion)
            .filter_by(id=version_id, project_id=project_id)
            .one_or_none()
        )

    def read_version_file(self, project_id: str, version_id: str, rel: str) -> str:
        """读版本内文件：先比对清单哈希，防止版本目录被外部篡改。"""
        version = self.get_version(project_id, version_id)
        if version is None:
            raise FileValidationError([f"版本不存在: {version_id!r}"])
        manifest = {f["path"]: f for f in json.loads(version.files_json)}
        if rel not in manifest:
            raise FileValidationError([f"文件不在版本清单中: {rel!r}"])
        _, target = self._resolve(self.version_root(project_id, version_id), rel)
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest[rel]["hash"]:
            raise FileValidationError([f"版本文件哈希不匹配（可能被篡改）: {rel!r}"])
        return data.decode("utf-8")


class VersionConflictError(Exception):
    """同内容哈希版本已存在。"""

    def __init__(self, existing_id: str) -> None:
        self.existing_id = existing_id
        super().__init__(f"duplicate content, existing version {existing_id}")


def _split_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """拆分 YAML frontmatter 与正文。无 frontmatter 返回 (None, text)。"""
    if not text.startswith("---"):
        return None, text
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        return None, text
    try:
        meta = yaml.safe_load(parts[0][3:])
    except yaml.YAMLError:
        return None, text
    if not isinstance(meta, dict):
        return None, text
    body = parts[1].lstrip("\n") if len(parts) > 1 else ""
    return meta, body


def _tree_hash(files: list[dict]) -> str:
    """规范化树哈希：路径排序，path:hash 拼接后再整体 sha256。"""
    material = "\n".join(f"{f['path']}:{f['hash']}" for f in sorted(files, key=lambda x: x["path"]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def version_view(v, with_files: bool = False) -> dict:  # noqa: ANN001
    """SkillVersion → 视图（路由与 Worker 处理器共用）。"""
    out = {
        "id": v.id,
        "label": v.label,
        "parent_version_id": v.parent_version_id,
        "brief_revision_id": v.brief_revision_id,
        "content_hash": v.content_hash,
        "warnings": json.loads(v.warnings_json),
        "status": v.status,
        "created_at": v.created_at.isoformat(),
    }
    if with_files:
        out["files"] = json.loads(v.files_json)
    return out
