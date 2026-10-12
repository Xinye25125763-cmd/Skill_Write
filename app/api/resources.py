"""上传资源 API（工程计划 §5 T06）：接收二进制/文本资源并追踪元信息。

- POST /api/projects/{pid}/resources：multipart 上传 → 保存到
  {data_dir}/resources/{resource_id}/{safe-filename}，元信息写入同目录
  meta.json（原名/哈希/大小/kind）；返回 resource_id/path/kind/sha256/size。
- GET /api/resources/{resource_id}：元信息查询（不回传内容本体）。

资源按 ID 保存，与项目草稿区（skills/drafts）完全隔离；文件名经
normalize_artifact_path 校验（拒绝穿越/绝对路径/控制字符），扩展名按
ResourcesConfig.allowed_extensions 策略检查，体积不超过 max_file_bytes。
"""
from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.projects import get_session
from app.core.config import load_config
from app.core.ulid import new_ulid
from app.generation.artifacts import ArtifactPathError, normalize_artifact_path
from app.models import Project

router = APIRouter(prefix="/api")

_KIND_TEXT_EXTS = frozenset({
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".ini", ".cfg",
})


def _resources_root() -> Path:
    return load_config().data_dir / "resources"


def _resource_dir(resource_id: str) -> Path:
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", resource_id):
        raise HTTPException(status_code=404, detail="resource not found")
    d = _resources_root() / resource_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="resource not found")
    return d


def _load_meta(d: Path) -> dict:
    meta_file = d / "meta.json"
    if not meta_file.is_file():
        raise HTTPException(status_code=404, detail="resource not found")
    return json.loads(meta_file.read_text(encoding="utf-8"))


@router.post("/projects/{pid}/resources", status_code=201)
async def upload_resource(
    pid: str,
    file: UploadFile,
    original_name: str = "",
    session: Session = Depends(get_session),
) -> dict:
    """上传资源：逐字节落盘并校验哈希；元信息持久化到 meta.json。"""
    if session.get(Project, pid) is None:
        raise HTTPException(status_code=404, detail="project not found")

    cfg = load_config()

    name = original_name or file.filename or ""
    try:
        rel = normalize_artifact_path(name)
    except ArtifactPathError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_filename", "message": str(exc)}) from exc

    suffix = rel.suffix.lower()
    if suffix and suffix not in cfg.resources.allowed_extensions:
        raise HTTPException(
            status_code=422,
            detail={"code": "disallowed_extension", "message": f"不允许的文件类型: {suffix}"},
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail={"code": "empty_file", "message": "上传内容为空"})
    if len(data) > cfg.resources.max_file_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "file_too_large",
                "message": f"文件超过上限 {cfg.resources.max_file_bytes} 字节",
            },
        )

    digest = sha256(data).hexdigest()
    kind = "text" if suffix in _KIND_TEXT_EXTS else "binary"

    resource_id = new_ulid()
    rdir = _resources_root() / resource_id
    rdir.mkdir(parents=True)
    stored = rdir.joinpath(*rel.parts)
    stored.write_bytes(data)

    # 落盘后回读校验（服务器文件真实存在且内容一致）
    if sha256(stored.read_bytes()).hexdigest() != digest:
        raise HTTPException(status_code=500, detail={"code": "storage_corruption", "message": "落盘后哈希校验失败"})

    meta = {
        "resource_id": resource_id,
        "project_id": pid,
        "original_name": rel.as_posix(),
        "path": rel.as_posix(),
        "kind": kind,
        "sha256": digest,
        "size": len(data),
    }
    (rdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return meta


@router.get("/resources/{resource_id}")
def get_resource(resource_id: str) -> dict:
    """资源元信息查询。"""
    d = _resource_dir(resource_id)
    return _load_meta(d)
