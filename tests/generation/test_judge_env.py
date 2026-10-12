"""v1.5 M5：分级模型环境变量验收。

SKILLFORGE_JUDGE_LLM_BASE_URL / _MODEL / _API_KEY 可为"裁判"调用
（盲评评审、触发验证判定）配置独立模型；未配置整体回落主模型，
不做半套配置的拼接（只配 URL 不配 MODEL → 仍用主模型）。
"""
from __future__ import annotations

import pytest

import app.workers.handlers.eval_run as er_mod
import app.workers.handlers.trigger_run as trig_mod
from app.llm import (
    OpenAICompatProvider,
    ProviderNotConfigured,
    judge_provider_from_env,
    provider_from_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例独立环境：清空全部模型环境变量。"""
    for var in (
        "SKILLFORGE_LLM_BASE_URL", "SKILLFORGE_LLM_MODEL", "SKILLFORGE_LLM_API_KEY",
        "SKILLFORGE_JUDGE_LLM_BASE_URL", "SKILLFORGE_JUDGE_LLM_MODEL",
        "SKILLFORGE_JUDGE_LLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_judge_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置了评审模型：裁判工厂返回它，而非主模型。"""
    monkeypatch.setenv("SKILLFORGE_LLM_BASE_URL", "https://main.example/v1")
    monkeypatch.setenv("SKILLFORGE_LLM_MODEL", "main-model")
    monkeypatch.setenv("SKILLFORGE_JUDGE_LLM_BASE_URL", "https://judge.example/v1")
    monkeypatch.setenv("SKILLFORGE_JUDGE_LLM_MODEL", "cheap-judge")
    monkeypatch.setenv("SKILLFORGE_JUDGE_LLM_API_KEY", "jk")

    p = judge_provider_from_env()
    assert isinstance(p, OpenAICompatProvider)
    assert p.model == "cheap-judge"
    assert p.base_url == "https://judge.example/v1"
    # 主工厂不受影响
    assert provider_from_env().model == "main-model"


def test_judge_env_falls_back_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置评审模型：回落主模型（现行为不变）。"""
    monkeypatch.setenv("SKILLFORGE_LLM_BASE_URL", "https://main.example/v1")
    monkeypatch.setenv("SKILLFORGE_LLM_MODEL", "main-model")

    p = judge_provider_from_env()
    assert p.model == "main-model"
    assert p.base_url == "https://main.example/v1"


def test_judge_env_partial_config_not_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """半套配置不拼接：只配了 judge URL 没配 MODEL → 整体回落主模型。"""
    monkeypatch.setenv("SKILLFORGE_LLM_BASE_URL", "https://main.example/v1")
    monkeypatch.setenv("SKILLFORGE_LLM_MODEL", "main-model")
    monkeypatch.setenv("SKILLFORGE_JUDGE_LLM_BASE_URL", "https://judge.example/v1")

    p = judge_provider_from_env()
    assert p.model == "main-model"
    assert p.base_url == "https://main.example/v1"


def test_judge_env_unset_both_raises_provider_not_configured() -> None:
    """两边都没配：维持 ProviderNotConfigured 语义（不静默造默认）。"""
    with pytest.raises(ProviderNotConfigured):
        judge_provider_from_env()


def test_judge_factories_wired_to_judge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """盲评评审与触发验证的模块级工厂默认走分级模型读取。"""
    assert er_mod.judge_provider_factory is judge_provider_from_env
    assert trig_mod.provider_factory is judge_provider_from_env

    monkeypatch.setenv("SKILLFORGE_LLM_BASE_URL", "https://main.example/v1")
    monkeypatch.setenv("SKILLFORGE_LLM_MODEL", "main-model")
    monkeypatch.setenv("SKILLFORGE_JUDGE_LLM_MODEL", "judge-model")
    monkeypatch.setenv("SKILLFORGE_JUDGE_LLM_BASE_URL", "https://judge.example/v1")

    assert er_mod.judge_provider_factory().model == "judge-model"
    assert trig_mod.provider_factory().model == "judge-model"
    # 生成/执行侧不受影响，仍读主模型
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    assert ar_mod.provider_factory is provider_from_env
    assert gs_mod.provider_factory is provider_from_env
    assert ar_mod.provider_factory().model == "main-model"
