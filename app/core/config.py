"""应用配置：config.yaml 加载，关键项可被环境变量覆盖。

环境变量：
    SKILLFORGE_CONFIG   配置文件路径
    SKILLFORGE_DATA_DIR 数据目录（测试与多环境隔离用）
    SKILLFORGE_PORT     API 端口
"""
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    """本地服务绑定配置。默认回环地址，禁止开放局域网。"""

    model_config = {"frozen": True}

    host: str = "127.0.0.1"
    port: int = 8710


class GenerationConfig(BaseModel):
    """生成流程配置（工程计划 T01）：默认工程规则路径与版本。

    只新增默认，不修改任何旧默认；相对路径同样受 F01 锦定规则约束。
    """

    model_config = {"frozen": True}

    profile_path: Path = Path("engineering/profiles/default.yaml")
    profile_id: str = "default"


class ResourcesConfig(BaseModel):
    """上传资源策略（工程计划 T06）：可配置格式与体积。

    默认允许文本扩展（与草稿白名单一致）加常见二进制格式；
    旧文本草稿接口（skill_files）行为不受本配置影响。
    """

    model_config = {"frozen": True}

    max_file_bytes: int = 20_000_000
    allowed_extensions: tuple[str, ...] = (
        ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
        ".py", ".js", ".ts", ".css", ".html",
        ".csv", ".sh", ".ps1", ".ini", ".cfg",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".pdf", ".zip",
    )


class AppConfig(BaseModel):
    """应用顶层配置。"""

    model_config = {"frozen": True}

    server: ServerConfig = Field(default_factory=ServerConfig)
    data_dir: Path = Path("data")
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)


def default_config_path() -> Path:
    """项目根目录下的 config.yaml。"""
    return Path(__file__).resolve().parents[2] / "config.yaml"


def _anchored_data_dir(data_dir: Path, config_root: Path) -> Path:
    """相对 data_dir 锚定到配置文件所在目录（项目根）并规范化为绝对路径。

    背景（F01）：相对路径受进程 cwd 影响，启动器（任意 cwd）与其
    cwd=ROOT 的子进程可能得到不同数据目录；工具层传给子进程的脚本
    路径也会被再次拼接。一律在加载时规范化为绝对路径。
    """
    if data_dir.is_absolute():
        return data_dir
    return (config_root / data_dir).resolve()


def load_config(path: Path | None = None) -> AppConfig:
    """加载配置；文件不存在时使用默认值；环境变量最后覆盖。

    data_dir 无论来自配置文件还是环境变量，相对路径一律锚定到
    配置文件所在目录（项目根）并规范化为绝对路径（F01）。
    """
    if path is None:
        env_cfg = os.environ.get("SKILLFORGE_CONFIG")
        path = Path(env_cfg) if env_cfg else default_config_path()

    raw: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is not None:
            raw = dict(loaded)

    cfg = AppConfig.model_validate(raw)
    config_root = path.resolve().parent

    if env_data := os.environ.get("SKILLFORGE_DATA_DIR"):
        cfg = cfg.model_copy(update={"data_dir": Path(env_data)})
    cfg = cfg.model_copy(update={"data_dir": _anchored_data_dir(cfg.data_dir, config_root)})
    if port := os.environ.get("SKILLFORGE_PORT"):
        cfg = cfg.model_copy(update={"server": cfg.server.model_copy(update={"port": int(port)})})
    return cfg
