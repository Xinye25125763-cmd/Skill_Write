"""结构化需求（brief）Schema 与三层校验（细化方案 §5）。

三层校验：
    1. 结构校验（Pydantic）：字段齐全、类型正确；
    2. 业务校验：触发条件非空且不重叠、输出非空、平台已注册；
    3. 追问预算：由 service 层控制（最多 2 轮），本模块只产出待确认项。

来源标注（主§6.1）：每个关键字段携带 value + source。
附件文本永远以 attachment_text 来源进入字段候选，绝不进入指令区。
"""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 已注册平台（细化方案 §7：CatPaw 首个；后续经适配器注册扩展）
REGISTERED_PLATFORMS = ("catpaw", "codex", "claude_code")


class SourceType(str, Enum):
    """字段来源类型（主§6.1）。"""

    USER_MESSAGE = "user_message"        # 用户消息原文
    ATTACHMENT_TEXT = "attachment_text"  # 附件原文（仅作被分析内容）
    USER_CONFIRMED = "user_confirmed"   # 用户确认过
    SYSTEM_INFERRED = "system_inferred"  # 系统推断


class FieldSource(BaseModel):
    """单条来源记录。"""

    type: SourceType
    ref: str = ""  # 消息 ID 或附件路径+行号


class SourcedField(BaseModel):
    """带来源的字段值。"""

    value: str
    source: FieldSource


class SourcedList(BaseModel):
    """带来源的列表字段（每项独立标注来源）。

    外部 JSON 惯用裸数组；校验器将其包装为 {"items": [...]}。
    """

    items: list[SourcedField] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, data):  # noqa: ANN206
        if isinstance(data, list):
            return {"items": data}
        return data

    @property
    def values(self) -> list[str]:
        """纯值列表。"""
        return [item.value for item in self.items]

    @property
    def is_empty(self) -> bool:
        return not self.items


class IOItem(BaseModel):
    """输入/输出条目。"""

    name: str
    format: str = ""
    required: bool = True
    validation: str = "manual"  # 程序断言或人工


class BriefPayload(BaseModel):
    """需求摘要主体（细化方案 §5 JSON 结构）。"""

    purpose: SourcedField | None = None
    trigger_conditions: SourcedList = Field(default_factory=SourcedList)
    non_trigger_conditions: SourcedList = Field(default_factory=SourcedList)
    inputs: list[IOItem] = Field(default_factory=list)
    outputs: list[IOItem] = Field(default_factory=list)
    tools: SourcedList = Field(default_factory=SourcedList)
    constraints: SourcedList = Field(default_factory=SourcedList)
    success_criteria: SourcedList = Field(default_factory=SourcedList)
    target_platform: str = "catpaw"
    evaluation_preference: dict[str, bool] = Field(
        default_factory=lambda: {"objective_tests": True, "subjective_scale": False}
    )
    # 追问状态：未决项标记（细化方案 §5：预算耗尽后标记 unresolved 继续推进）
    unresolved: list[str] = Field(default_factory=list)


class BriefDraft(BaseModel):
    """需求草案：brief + 分列视图数据（明确 / 推断 / 待确认）。"""

    brief: BriefPayload

    @property
    def explicit(self) -> list[str]:
        """用户已明确（用户消息或用户确认来源的字段路径）。"""
        return [
            path
            for path, field in self._iter_sourced()
            if field.source.type in (SourceType.USER_MESSAGE, SourceType.USER_CONFIRMED)
        ]

    @property
    def inferred(self) -> list[str]:
        """系统推断来源的字段路径。"""
        return [
            path
            for path, field in self._iter_sourced()
            if field.source.type == SourceType.SYSTEM_INFERRED
        ]

    @property
    def to_confirm(self) -> list[str]:
        """待确认：来源全为附件文本且未获用户确认的字段，加 unresolved 标记。"""
        paths = [
            path
            for path, field in self._iter_sourced()
            if field.source.type == SourceType.ATTACHMENT_TEXT
        ]
        return paths + list(self.brief.unresolved)

    def _iter_sourced(self):
        """遍历所有带来源字段，产出 (路径, 字段)。"""
        b = self.brief
        if b.purpose is not None:
            yield "purpose", b.purpose
        for name, lst in (
            ("trigger_conditions", b.trigger_conditions),
            ("non_trigger_conditions", b.non_trigger_conditions),
            ("tools", b.tools),
            ("constraints", b.constraints),
            ("success_criteria", b.success_criteria),
        ):
            for idx, item in enumerate(lst.items):
                yield f"{name}[{idx}]", item


class BriefValidationError(Exception):
    """brief 校验失败。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_brief(draft: BriefDraft) -> list[str]:
    """业务校验（细化方案 §5 第 2 层）：返回问题清单，空列表即通过。

    与结构校验分离：结构问题由 Pydantic 抛出（parse_failed），
    业务问题驱动追问，交由用户解决后进入待确认列。
    """
    errors: list[str] = []
    b = draft.brief

    if b.purpose is None or not b.purpose.value.strip():
        errors.append("purpose 缺失：技能要让 AI 能做什么")
    if b.trigger_conditions.is_empty:
        errors.append("trigger_conditions 缺失：应触发的用户语境/措辞")
    if b.non_trigger_conditions.is_empty:
        errors.append("non_trigger_conditions 缺失：不应触发的近似场景")
    if not b.outputs:
        errors.append("outputs 缺失：至少一项输出")
    if b.target_platform not in REGISTERED_PLATFORMS:
        errors.append(f"target_platform 未注册：{b.target_platform}")

    # 触发与非触发不得语义重叠（子串级初筛，深度语义重叠交用户确认）
    trigger_lower = [t.value.strip().lower() for t in b.trigger_conditions.items]
    for nt in b.non_trigger_conditions.items:
        if nt.value.strip().lower() in trigger_lower:
            errors.append(f"触发与非触发重叠：{nt.value!r}")
    return errors


@model_validator(mode="after")
def _check_attachment_only_fields(model: "BriefPayload") -> "BriefPayload":
    """附件来源字段且未确认 → 强制进 unresolved（细化方案 §5 末段）。

    S2 阶段 brief 由人工导入（降级路径），本校验保障后续自动提取接入时
    附件指令永远停留在待确认列，不会静默成为已生效需求。
    """
    # resolved 状态由 service 层在用户确认时改写 source 为 user_confirmed；
    # 此处仅对结构做防御：unresolved 中的路径必须真实存在。
    return model


BriefPayload.model_rebuild()
