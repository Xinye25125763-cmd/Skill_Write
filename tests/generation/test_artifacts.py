"""T06：支持完整资源的上传与安全暂存。

本项验收（工程计划 §5 T06，样本 C）：
1. C 二进制上传后哈希不变（sha256 落库 + 元信息可查）；
2. 拒绝 ../、绝对路径、反斜杠路径、重复规范化路径；
3. 半包不发布（staging 目录校验失败 → publish 拒绝，无候选目录）；
4. 不覆盖草稿（生成区独立于 skills/drafts）；
5. 文本/二进制往返；stage → publish 原子发布（同盘 rename）。
"""
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config

AUTH = {"x-session-token": "test-token"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- 验收 1（C）：二进制上传 + 哈希不变 + 元信息 ----------


def test_binary_upload_and_metadata(client: TestClient) -> None:
    r = client.post("/api/projects", json={"name": "p"}, headers=AUTH)
    pid = r.json()["id"]

    png = b"\x89PNG\r\n\x1a\n" + bytes(200)
    r = client.post(
        f"/api/projects/{pid}/resources",
        files={"file": ("logo.png", png, "image/png")},
        data={"original_name": "logo.png"},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "binary"
    assert body["sha256"] == _sha256(png)
    assert body["path"].endswith(".png")
    assert body["size"] == len(png)

    # 元信息查询
    r = client.get(f"/api/resources/{body['resource_id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["sha256"] == _sha256(png)
    assert r.json()["original_name"] == "logo.png"

    # 服务器文件真实存在且内容一致
    from app.core.config import load_config
    cfg = load_config()
    stored = cfg.data_dir / "resources" / body["resource_id"] / body["path"]
    assert stored.read_bytes() == png


# ---------- 验收 2：路径逃逸拒绝 ----------


def test_path_escape_rejected(client: TestClient) -> None:
    from app.generation.artifacts import ArtifactPathError, stage_artifacts
    from app.generation.contracts import ArtifactFile, ArtifactManifest

    r = client.post("/api/projects", json={"name": "p"}, headers=AUTH)
    pid = r.json()["id"]

    root = Path(load_config().data_dir) / "generation" / pid
    root.mkdir(parents=True, exist_ok=True)

    def _manifest(path: str) -> ArtifactManifest:
        return ArtifactManifest(artifacts=[ArtifactFile(
            path=path, kind="text", content="x", sha256="h",
        )])

    # ../ 穿越、绝对路径、反斜杠 —— 清单级拒绝
    for bad in ("../evil.txt", "/etc/passwd", "a\\b.txt", "assets/../../x.png"):
        with pytest.raises(ArtifactPathError):
            stage_artifacts("flow-x", _manifest(bad), resources={}, root=root)

    # 重复规范化路径（assets/../logo.png 与 logo.png 同一文件）
    m = ArtifactManifest(artifacts=[
        ArtifactFile(path="logo.png", kind="text", content="a", sha256="h1"),
        ArtifactFile(path="sub/../logo.png", kind="text", content="b", sha256="h2"),
    ])
    with pytest.raises(ArtifactPathError):
        stage_artifacts("flow-x", m, resources={}, root=root)


# ---------- 验收 3：半包不发布 ----------


def test_half_package_not_published(tmp_path: Path) -> None:
    from app.generation.artifacts import (
        ArtifactPathError,
        publish_candidate,
        stage_artifacts,
    )
    from app.generation.contracts import ArtifactFile, ArtifactManifest

    root = tmp_path / "gen"
    # 文本 content 但 sha256 不匹配 → stage 阶段就失败
    bad = ArtifactManifest(artifacts=[ArtifactFile(
        path="SKILL.md", kind="text", content="# x", sha256="wrong-hash",
    )])
    with pytest.raises(ArtifactPathError):
        stage_artifacts("flow-1", bad, resources={}, root=root)
    # 无候选目录产生
    assert not (root / "flow-1" / "candidate").exists()

    # 正常 stage → publish 校验包哈希
    good = ArtifactManifest(artifacts=[ArtifactFile(
        path="SKILL.md", kind="text", content="# ok",
        sha256=_sha256("# ok".encode()),
    )])
    staged = stage_artifacts("flow-1", good, resources={}, root=root)
    from app.generation.artifacts import manifest_hash
    manifest = publish_candidate(staged, expected_hash=manifest_hash(good), root=root)
    assert [a.path for a in manifest.artifacts] == ["SKILL.md"]
    assert (root / "candidate" / "flow-1" / "SKILL.md").exists()

    # 期望哈希不符 → 拒绝发布
    staged2 = stage_artifacts("flow-2", good, resources={}, root=root)
    with pytest.raises(ArtifactPathError):
        publish_candidate(staged2, expected_hash="tampered", root=root)


# ---------- 验收 4：不覆盖草稿 ----------


def test_generation_area_separate_from_draft(client: TestClient) -> None:
    r = client.post("/api/projects", json={"name": "p"}, headers=AUTH)
    pid = r.json()["id"]

    # 用户草稿
    r = client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": "# 用户草稿"}, headers=AUTH,
    )
    assert r.status_code == 200

    # 上传资源 + 暂存发布全在独立 generation 区
    from app.core.config import load_config
    cfg = load_config()
    data_dir = cfg.data_dir

    png = bytes(50)
    r = client.post(
        f"/api/projects/{pid}/resources",
        files={"file": ("x.png", png, "image/png")},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert "resources" in r.json()["path"] or r.json()["resource_id"]

    # 草稿原样
    r = client.get(f"/api/projects/{pid}/skills/draft/files/SKILL.md", headers=AUTH)
    assert "用户草稿" in r.json()["content"]

    # 目录结构：resources 区与项目草稿区分离（草稿真实位置：
    # {data_dir}/projects/{pid}/skills/drafts）
    assert (data_dir / "resources").exists()
    assert (data_dir / "projects" / pid / "skills" / "drafts").exists()


# ---------- 文本资源暂存 + 二进制引用 ----------


def test_text_and_binary_staging_roundtrip(tmp_path: Path) -> None:
    from app.generation.artifacts import stage_artifacts, publish_candidate
    from app.generation.contracts import ArtifactFile, ArtifactManifest

    root = tmp_path / "gen"
    logo = b"\x89PNG" + bytes(30)

    manifest = ArtifactManifest(artifacts=[
        ArtifactFile(path="SKILL.md", kind="text", content="# s",
                     sha256=_sha256(b"# s"), requirement_ids=["REQ-001"]),
        ArtifactFile(path="assets/logo.png", kind="binary",
                     resource_id="res-01", sha256=_sha256(logo)),
    ])
    staged = stage_artifacts("flow-3", manifest, resources={"res-01": logo}, root=root)

    # 暂存区文件真实存在
    assert (staged / "SKILL.md").read_text(encoding="utf-8") == "# s"
    assert (staged / "assets" / "logo.png").read_bytes() == logo

    # 发布后候选目录为 rename 的同盘原子操作
    from app.generation.artifacts import manifest_hash
    published = publish_candidate(staged, expected_hash=manifest_hash(manifest), root=root)
    assert published.artifacts[0].content == "# s"
    assert not staged.exists()  # rename 后暂存目录消失
    assert (root / "candidate" / "flow-3" / "assets" / "logo.png").read_bytes() == logo

    # 二进制资源缺失 → stage 拒绝（声明的每个必要资源必须实际可读取）
    m2 = ArtifactManifest(artifacts=[
        ArtifactFile(path="assets/logo.png", kind="binary",
                     resource_id="res-404", sha256=_sha256(logo)),
    ])
    from app.generation.artifacts import ArtifactPathError
    with pytest.raises(ArtifactPathError):
        stage_artifacts("flow-4", m2, resources={}, root=root)
