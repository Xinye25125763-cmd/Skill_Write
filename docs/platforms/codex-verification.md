# codex 宿主验证文档（T18）

## 适配层位置

- `app/platforms/base.py`：HostAdapter 基类 + 注册表
- `app/platforms/codex.py`：codex 适配器实现

## 能力声明

| 能力 | 值 | 说明 |
|---|---|---|
| `native_trigger` | `True` | codex 走原生触发（CLI 子进程），非 API 注入 |
| `verified` | 依赖 CLI 可用 | CLI 在 PATH 中即声明可验证 |
| `platform` | `"codex"` | 个人目标宿主 |

## 真实宿主验收步骤（人工或自动采集证据）

1. **CLI 探活**：`codex --version` 返回 0 退出 → host:codex:available = PASSED
2. **正触发**：向 codex 发送匹配 SKILL.md 触发条件的提示词 → 技能被命中 → records.json 记录 `source=codex-cli, hit=true`
3. **负触发**：发送不匹配提示词 → 技能未命中 → records.json 记录 `source=codex-cli, hit=false`
4. **资源加载**：技能引用 reference.md → codex 读取并输出内容 → records.json 记录 `files_loaded=["reference.md"]`
5. **脚本条件**：技能引用 scripts/extract.py → codex 执行 → records.json 记录 `scripts_run=["scripts/extract.py"], exit_code=0`

## 安全保证

- **API 注入不可冒充**：records.json 中 `source` 必须为 `"codex-cli"`，其他 source 值导致 FAILED
- **不可用 → blocked**：CLI 缺失或探活失败 → 全部 host: 检查 NOT_RUN/UNSUPPORTED
- **其他平台不自动标支持**：注册占位适配器（`verified=False, native_trigger=False`）

## 执行记录格式

```json
[
  {
    "case_id": "trigger-positive",
    "source": "codex-cli",
    "hit": true,
    "skill": "test-skill",
    "prompt": "帮我整理会议纪要"
  },
  {
    "case_id": "resource-load",
    "source": "codex-cli",
    "hit": true,
    "skill": "test-skill",
    "files_loaded": ["reference.md"],
    "output": "标题：测试技能"
  },
  {
    "case_id": "script-condition",
    "source": "codex-cli",
    "hit": true,
    "skill": "test-skill",
    "scripts_run": ["scripts/extract.py"],
    "output": "extract",
    "exit_code": 0
  }
]
```

## 测试覆盖

`tests/platforms/test_codex_adapter.py` — `test_host_unavailable_not_pass_and_trigger_positive_negative`

覆盖：不可用→blocked、正/负触发、资源加载证据、脚本条件证据、API注入拒绝、负例失败如实。
