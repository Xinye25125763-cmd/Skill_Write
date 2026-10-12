"""文本/二进制资源接收、暂存、完整性与原子发布（工程计划 §5 T06）。

目录约定（与草稿区完全隔离，绝不覆盖草稿）：
    {root}/staging/{flow_id}/    暂存区（含 .manifest.json）
    {root}/candidate/{flow_id}/  发布候选（同盘 rename，原子）

安全纪律：
- 产物路径自含校验：拒绝 ../ 穿越、绝对路径、反斜杠、控制字符、盘符；
  不复用文本草稿的扩展名白名单——二进制产物（png 等）是合法路径；
- 重复规范化路径拒绝；
- 文本逐文件 sha256 校验；二进制必须来自已上传资源（resources 装载字节），
  声明的资源缺失即拒绝——"声明的每个必要资源必须实际可读取"；
- 半包不发布：stage 任一文件失败 → 整包拒绝并清理暂存目录；
- stage 全部校验通过后把 manifest 序列化写入 .manifest.json；
- publish 校验 manifest 哈希（sha256(序列化 JSON)）后才同盘 rename。
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from hashlib import sha256
from pathlib import Path, PurePosixPath

from app.generation.contracts import (
    ArtifactFile,
    ArtifactKind,
    ArtifactManifest,
)


class ArtifactPathError(Exception):
    """产物路径非法 / 哈希不符 / 资源缺失 / 半包——任何 stage/publish 拒绝。"""


def normalize_artifact_path(raw: str) -> PurePosixPath:
    """产物路径校验（T06）：穿越/绝对/反斜杠/控制字符/盘符拒绝，不限扩展名。"""
    if not raw or not raw.strip():
        raise ArtifactPathError(f"产物路径为空: {raw!r}")
    if any(ord(ch) < 0x20 for ch in raw):
        raise ArtifactPathError(f"产物路径含控制字符: {raw!r}")
    if "\\" in raw:
        raise ArtifactPathError(f"产物路径含反斜杠（统一正斜杠）: {raw!r}")
    raw = unicodedata.normalize("NFC", raw)
    p = PurePosixPath(raw)
    if p.is_absolute() or raw.startswith("/"):
        raise ArtifactPathError(f"拒绝绝对路径: {raw!r}")
    if re.match(r"^[A-Za-z]:", raw):
        raise ArtifactPathError(f"拒绝盘符路径: {raw!r}")
    if any(part == ".." for part in p.parts):
        raise ArtifactPathError(f"拒绝路径穿越: {raw!r}")
    if any(part in (".", "") for part in p.parts):
        raise ArtifactPathError(f"拒绝特殊路径段: {raw!r}")
    if not p.parts or p.parts == ("",):
        raise ArtifactPathError(f"产物路径为空: {raw!r}")
    return p


def _safe_relpath(raw: str) -> PurePosixPath:
    return normalize_artifact_path(raw)


def _check_no_duplicate_normalized(manifest: ArtifactManifest) -> None:
    seen: set[str] = set()
    for a in manifest.artifacts:
        rel = _safe_relpath(a.path)
        norm = str(PurePosixPath(*rel.parts))  # 规范化后比较
        if norm in seen:
            raise ArtifactPathError(f"重复规范化路径: {a.path} → {norm}")
        seen.add(norm)


def stage_artifacts(
    flow_id: str,
    manifest: ArtifactManifest,
    resources: dict[str, bytes],
    root: Path,
) -> Path:
    """把产物清单落到暂存区，返回暂存目录。

    逐文件校验（文本 sha256 / 二进制资源存在且哈希一致），全包回读复核，
    最后写入 .manifest.json；任一失败整包拒绝并清理暂存目录（半包不发布）。
    """
    _check_no_duplicate_normalized(manifest)

    staging = root / "staging" / flow_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        for art in manifest.artifacts:
            rel = _safe_relpath(art.path)
            target = staging.joinpath(*rel.parts)
            # is_relative_to 双保险（F05 纪律）
            if not target.resolve().is_relative_to(staging.resolve()):
                raise ArtifactPathError(f"路径越出暂存区: {art.path!r}")

            if art.kind == ArtifactKind.TEXT:
                data = (art.content or "").encode("utf-8")
            else:
                data = resources.get(art.resource_id or "")
                if data is None:
                    raise ArtifactPathError(
                        f"二进制产物引用的资源缺失: {art.path} → {art.resource_id!r}"
                    )

            actual = sha256(data).hexdigest()
            if art.sha256 != actual:
                raise ArtifactPathError(
                    f"产物哈希不符: {art.path}（声明 {art.sha256[:12]}… 实际 {actual[:12]}…）"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            if art.kind == ArtifactKind.TEXT:
                target.write_text(art.content or "", encoding="utf-8", newline="\n")
            else:
                target.write_bytes(data)

        # 全包完整性再校验：逐文件回读哈希（磁盘真实内容，非内存值）
        for art in manifest.artifacts:
            rel = _safe_relpath(art.path)
            target = staging.joinpath(*rel.parts)
            data = target.read_bytes()
            if sha256(data).hexdigest() != art.sha256:
                raise ArtifactPathError(f"暂存后回读哈希不符: {art.path}")

        # 全部通过 → 写 manifest（其 sha256 即 publish 的 expected_hash）
        payload = manifest.model_dump_json()
        (staging / ".manifest.json").write_text(payload, encoding="utf-8", newline="\n")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)  # 半包清理
        raise

    return staging


def manifest_hash(manifest: ArtifactManifest) -> str:
    """发布校验用的期望哈希：与 stage 写入 .manifest.json 的序列化一致。"""
    return sha256(manifest.model_dump_json().encode("utf-8")).hexdigest()


def publish_candidate(
    staged: Path, expected_hash: str, root: Path
) -> ArtifactManifest:
    """把暂存区发布为候选目录（同盘 rename，原子）。

    expected_hash：调用方持有的 manifest 序列化哈希（manifest_hash）。
    校验失败 / 暂存区被篡改 → 拒绝发布且不产生候选目录。
    """
    manifest_file = staged / ".manifest.json"
    if not manifest_file.is_file():
        raise ArtifactPathError(f"暂存区缺少 manifest: {staged}")
    raw = manifest_file.read_text(encoding="utf-8")
    if sha256(raw.encode("utf-8")).hexdigest() != expected_hash:
        raise ArtifactPathError("manifest 哈希与期望不符，拒绝发布（可能被篡改）")

    manifest = ArtifactManifest.model_validate_json(raw)

    candidate = root / "candidate" / staged.name
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        shutil.rmtree(candidate)  # 修复轮重发布
    staged.rename(candidate)

    # 候选目录回读重建 manifest（发布的是已验证的磁盘内容）
    rebuilt: list[ArtifactFile] = []
    for art in manifest.artifacts:
        rel = _safe_relpath(art.path)
        data = (candidate.joinpath(*rel.parts)).read_bytes()
        rebuilt.append(art.model_copy(update={"sha256": sha256(data).hexdigest()}))
    return ArtifactManifest(artifacts=rebuilt)
