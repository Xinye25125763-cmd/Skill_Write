"""把生成候选冻结成可复现版本（工程计划 §5 T10）。

与草稿创建版本（SkillFileService.create_version）的关键差异：
- 入口是已验证的独立候选目录（generation 区），绝不写回共享草稿；
- 对账纪律：复制前后双向哈希对账——源目录与版本目录逐文件一致才落库，
  复制竞态失败（任一不一致）不写有效版本（无版本行 + 清理临时目录）；
- 版本关联合同 JSON/合同哈希/规则哈希/验证报告/来源流程（0009 迁移列）；
- 二进制以字节原样复制，清单 kind=binary（read_version_file_bytes 可读）。
"""
from __future__ import annotations

import json
import shutil
from hashlib import sha256
from pathlib import Path

from app.core.config import AppConfig
from app.core.ulid import new_ulid
from app.generation.contracts import (
    ArtifactKind,
    ArtifactManifest,
    CheckReport,
    RequirementContract,
)
from app.models import SkillVersion, utcnow


class FreezeError(Exception):
    """冻结失败：对账不一致 / 空候选 / 复制竞态。不写任何有效版本。"""


def _tree_manifest(root: Path) -> list[dict]:
    """磁盘实际文件树清单（路径排序 + 哈希 + 大小 + kind）。

    排除 __pycache__ 与 .pyc：脚本 smoke 在暂存目录内执行时 Python 会在
    同目录落下字节码缓存，它们不是技能产物（真实链路抓到的缺陷：
    缓存文件混进冻结版本，还把 pyc 误判为 binary 资源）。
    """
    out: list[dict] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.name == ".manifest.json":
            continue
        if "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        rel = f.relative_to(root).as_posix()
        data = f.read_bytes()
        out.append({
            "path": rel,
            "hash": sha256(data).hexdigest(),
            "size": len(data),
            "kind": "binary" if f.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".svg",
            } else "text",
        })
    return out


def _tree_hash(files: list[dict]) -> str:
    material = "\n".join(f"{f['path']}:{f['hash']}" for f in sorted(files, key=lambda x: x["path"]))
    return sha256(material.encode("utf-8")).hexdigest()


def freeze_candidate(
    session,  # noqa: ANN001
    flow_id: str,
    manifest: ArtifactManifest,
    staged: Path,
    *,
    contract: RequirementContract | None = None,
    profile_hash: str | None = None,
    report: CheckReport | None = None,
    cfg: AppConfig | None = None,
    project_id: str | None = None,
) -> str:
    """把候选目录冻结为不可变 SkillVersion，返回 version_id。

    - staged 必须存在且与 manifest 对账（逐文件哈希一致）；
    - 复制到 versions/{vid}.tmp → 对账 → 原子 rename → 落库；
    - 任一对账失败 → FreezeError，无版本行、无残留版本目录。
    """
    if not staged.is_dir():
        raise FreezeError(f"候选目录不存在: {staged}")
    if project_id is None:
        from app.generation.flow import FlowNotFound, get_flow
        try:
            snap = get_flow(session, flow_id)
            project_id = snap.project_id
        except FlowNotFound as exc:
            raise FreezeError(f"流程不存在，无法确定项目: {flow_id}") from exc

    # 源对账：manifest 声明 vs 磁盘实际
    disk = {f["path"]: f for f in _tree_manifest(staged)}
    for art in manifest.artifacts:
        actual = disk.get(art.path)
        if actual is None:
            raise FreezeError(f"候选缺少声明文件: {art.path}")
        if actual["hash"] != art.sha256:
            raise FreezeError(
                f"候选哈希与清单不符: {art.path}（复制前对账失败）"
            )

    files = sorted(disk.values(), key=lambda x: x["path"])
    if not files:
        raise FreezeError("空候选：至少需要 SKILL.md")
    if "SKILL.md" not in {f["path"] for f in files}:
        raise FreezeError("候选缺少 SKILL.md")

    content_hash = _tree_hash(files)

    # 同内容冲突：既有版本直接复用（幂等冻结）
    existing = (
        session.query(SkillVersion)
        .filter_by(project_id=project_id, content_hash=content_hash)
        .one_or_none()
    )
    if existing is not None:
        return existing.id

    from app.services.skill_files import SkillFileService
    svc = SkillFileService(cfg, session) if cfg is not None else None
    version_id = new_ulid()
    skills_root = (
        svc._project_skills_root(project_id) if svc is not None
        else Path("data") / "projects" / project_id / "skills"
    )
    final_dir = skills_root / "versions" / version_id
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)

    # 复制（跳过 .manifest.json——它不是技能的一部分）
    shutil.copytree(
        staged, tmp_dir,
        ignore=shutil.ignore_patterns(".manifest.json", "__pycache__", "*.pyc"),
    )

    # 复制后对账：源目录与临时版本目录逐文件一致（复制竞态围栏）
    src_files = {f["path"]: f["hash"] for f in files}
    copied = {f["path"]: f["hash"] for f in _tree_manifest(tmp_dir)}
    if copied != src_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise FreezeError("复制后对账失败（候选在复制期间被修改），未写版本")

    tmp_dir.rename(final_dir)  # 同盘原子重命名

    contract_json = contract.model_dump_json() if contract is not None else None
    contract_hash = (
        sha256(contract_json.encode("utf-8")).hexdigest() if contract_json else None
    )
    version = SkillVersion(
        id=version_id,
        project_id=project_id,
        label=None,
        content_hash=content_hash,
        files_json=json.dumps(files, ensure_ascii=False),
        warnings_json="[]",
        status="validated",
        created_at=utcnow(),
        generation_flow_id=flow_id,
        contract_json=contract_json,
        contract_hash=contract_hash,
        profile_hash=profile_hash,
        report_json=report.model_dump_json() if report is not None else None,
    )
    session.add(version)
    session.commit()
    return version_id
