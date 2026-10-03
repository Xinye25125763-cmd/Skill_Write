"""S2 测试夹具：临时数据目录 + 迁移后数据库 + API 客户端。"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.config import load_config
from app.core.db import db_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def _root() -> Path:
    return ROOT


@pytest.fixture()
def migrated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """建库并跑全部迁移。"""
    monkeypatch.setenv("SKILLFORGE_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    alembic_cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
    return db_path(cfg)


@pytest.fixture()
def client(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """鉴权 API 客户端，且路由层复用迁移后的数据库文件。"""
    monkeypatch.setenv("SKILLFORGE_TOKEN", "test-token")
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def project(client: TestClient) -> dict:
    """预建一个项目。"""
    resp = client.post(
        "/api/projects",
        json={"name": "测试项目", "target_platform": "catpaw"},
        headers={"x-session-token": "test-token"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


VALID_BRIEF = {
    "purpose": {
        "value": "把会议纪要整理成结构化行动项列表",
        "source": {"type": "user_message", "ref": "msg-1"},
    },
    "trigger_conditions": [
        {"value": "帮我整理会议纪要", "source": {"type": "user_message", "ref": "msg-1"}}
    ],
    "non_trigger_conditions": [
        {"value": "帮我写会议通知", "source": {"type": "system_inferred", "ref": ""}}
    ],
    "inputs": [{"name": "纪要文本", "format": "text", "required": True, "validation": "manual"}],
    "outputs": [{"name": "行动项列表", "format": "markdown", "required": True, "validation": "manual"}],
    "tools": [],
    "constraints": [
        {"value": "输出用中文", "source": {"type": "user_message", "ref": "msg-2"}}
    ],
    "success_criteria": [
        {"value": "每个行动项含负责人与截止日", "source": {"type": "user_confirmed", "ref": ""}}
    ],
    "target_platform": "catpaw",
    "evaluation_preference": {"objective_tests": True, "subjective_scale": False},
}
