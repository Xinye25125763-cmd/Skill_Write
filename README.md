# Skill_Write

Skill Forge v1.1：个人技能需求整理、版本管理、执行与对照评测工作台。

## 先看这里

- [对外快速介绍](deliverables/Skill-Forge-v1.1-完整交付-2026-10-03/05-流程详解与对外介绍/Skill-Forge-对外快速介绍.md)
- [内部完整流程与实现详解](deliverables/Skill-Forge-v1.1-完整交付-2026-10-03/05-流程详解与对外介绍/Skill-Forge-v1.1-内部完整流程与实现详解.md)
- [完整交付文件索引](deliverables/Skill-Forge-v1.1-完整交付-2026-10-03/交付文件索引.md)
- [修改、验证与升级说明](deliverables/Skill-Forge-v1.1-完整交付-2026-10-03/02-最终说明与评估/skill-forge-v1.1修改与验证报告.md)
- [前置skill适配评估](deliverables/Skill-Forge-v1.1-完整交付-2026-10-03/02-最终说明与评估/前置skill适配评估.md)

## 使用源码

源码目录：`deliverables/Skill-Forge-v1.1-完整交付-2026-10-03/01-源码与发布包/skill-forge-v1.1-source/`。

进入该目录，按README安装依赖，执行：

```bash
uv sync
uv run python launcher.py
```

交付包含源码ZIP、重新构建的前端、需求JSON示例、完整文档、设计、审计记录和SHA256校验清单。个人运行数据、密钥、依赖目录及操作系统缓存不上传。

当前后端验证记录为134项通过、1项跳过，前端检查和构建通过；未调用真实模型端点。自动生成、宿主触发优化、盲评和自动安装仍未实现。前置skill是接入设计，尚未创建或安装。
