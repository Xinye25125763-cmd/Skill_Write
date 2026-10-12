"""生成流程的可验证类型（工程计划 §3，T01）。

八个核心模型贯穿生成全链：
    RequirementContract  需求合同（功能 ID + 来源 + 确认状态 + 验收 ID）
    EngineeringProfile   版本化工程规则（rule_id + severity + 检查器）
    SkillPlan            工程/测试方案（功能 → 文件 → 工具 → 验收映射，T05 填充）
    ArtifactManifest     产物清单（path/kind/sha256/资源引用/需求关联）
    CheckReport          检查报告（check_id/status/evidence/artifact_hash）
    FlowBudget           全流程预算（token/调用/修复轮次，负数非法）
    FlowSnapshot         流程快照（状态机 + 乐观并发 revision）
    DeliveryReceipt      交付收据（五维状态分离）

设计纪律（全局约束）：
- 所有模型带 schema_version，序列化往返不得丢失来源与 ID；
- 枚举一律封闭（未知值拒绝），重复 ID 拒绝，预算负数拒绝；
- 附件来源（ATTACHMENT_TEXT）永远只是被分析内容，不是指令。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1"


# ---------- 共享枚举 ----------


class SourceType(str, Enum):
    """需求来源类型（与 brief_schema.SourceType 同构，独立定义避免耦合）。"""

    USER_MESSAGE = "user_message"        # 用户消息原文
    ATTACHMENT_TEXT = "attachment_text"  # 附件原文（仅作被分析内容）
    USER_CONFIRMED = "user_confirmed"   # 用户确认过
    SYSTEM_INFERRED = "system_inferred"  # 系统推断


class Confirmation(str, Enum):
    """需求确认状态。"""

    CONFIRMED = "confirmed"  # 用户确认
    PENDING = "pending"      # 待确认（必要缺口 → awaiting_input）
    INFERRED = "inferred"    # 非必要：注明推断后推进


class Severity(str, Enum):
    """工程规则严重级别。"""

    BLOCKING = "blocking"  # 违反即不通过
    WARNING = "warning"    # 提示，可被用户明确覆盖


class CheckStatus(str, Enum):
    """检查结果状态：未执行的检查只能落这两个值，绝不能写 passed。"""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"        # 有检查器但未运行（预算/环境）
    UNSUPPORTED = "unsupported"  # 无对应检查器/宿主能力缺失


class ArtifactKind(str, Enum):
    """产物文件类型。"""

    TEXT = "text"
    BINARY = "binary"


class FlowState(str, Enum):
    """生成流程状态机（工程计划 §3 FlowSnapshot 状态）。"""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    AWAITING_INPUT = "awaiting_input"  # 必要缺口，等用户回答
    PLANNING = "planning"
    GENERATING = "generating"
    VALIDATING = "validating"
    EVALUATING = "evaluating"
    REPAIRING = "repairing"
    HOST_CHECKING = "host_checking"
    PACKAGING = "packaging"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # 宕机恢复标记（沿用队列语义）


class DeliveryStatus(str, Enum):
    """交付状态（§2：候选与已交付分离）。"""

    CANDIDATE = "candidate"  # 可导出的未验证候选
    DELIVERED = "delivered"  # 用户要求的必要检查全过


# ---------- 基础构件 ----------


class SourceRef(BaseModel):
    """来源记录：类型 + 引用（消息 ID / 附件路径+行号）。"""

    model_config = ConfigDict(frozen=True)

    type: SourceType
    ref: str = ""


def _reject_duplicate_ids(items: list[str], what: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            raise ValueError(f"重复{what} ID: {item!r}（duplicate id）")
        seen.add(item)


# ---------- 需求合同 ----------


class Requirement(BaseModel):
    """一条用户功能需求（合同的最小单元）。

    - requirement_id 稳定：后续追问/修复/覆盖报告全部按此 ID 引用；
    - necessary=True 的功能缺失 → blocking，不能交付；
    - source 记录出处；ATTACHMENT_TEXT 只是候选来源，须确认才生效；
    - acceptance_ids 指向 SkillPlan 中的验收项（T05 填充）。
    """

    requirement_id: str = Field(min_length=1, pattern=r"^REQ-[0-9A-Za-z_.-]+$")
    content: str = Field(min_length=1)
    necessary: bool = True
    source: SourceRef
    confirmation: Confirmation = Confirmation.PENDING
    acceptance_ids: list[str] = Field(default_factory=list)
    note: str = ""  # 推断依据/冲突说明等

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_source(cls, data: Any) -> Any:
        """兼容扁平 source_type/source_ref 输入（模型结构化输出的常见形态）。"""
        if isinstance(data, dict) and "source" not in data:
            st = data.get("source_type")
            if isinstance(st, str):
                data = {**data, "source": {"type": st, "ref": str(data.get("source_ref") or "")}}
        return data


class RequirementContract(BaseModel):
    """需求合同：一次生成请求的全部功能需求。"""

    schema_version: str = SCHEMA_VERSION
    request_id: str = Field(min_length=1)  # 发起 flow_id
    target_platform: str = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    conflicts: list[str] = Field(default_factory=list)  # 冲突描述，不静默消失
    # 用户对工程规则的显式覆盖请求（仅 user 来源生效；附件来源一律拒绝，T02）
    rule_overrides: list["RuleOverride"] = Field(default_factory=list)

    @field_validator("requirements")
    @classmethod
    def _unique_requirement_ids(cls, v: list[Requirement]) -> list[Requirement]:
        _reject_duplicate_ids([r.requirement_id for r in v], "需求")
        return v

    # T04：提取阶段的问题与缺口判定
    open_questions: list[str] = Field(default_factory=list)

    def with_questions(self, questions: list[str]) -> "RequirementContract":
        """附上提取阶段的问题清单（不可变拷贝）。"""
        return self.model_copy(update={"open_questions": list(questions)})

    def unresolved_gaps(self) -> list[Requirement]:
        """必要缺口：necessary 且未经用户确认的需求。"""
        return [
            r for r in self.requirements
            if r.necessary and r.confirmation != Confirmation.CONFIRMED
        ]


class RuleOverride(BaseModel):
    """对一条工程规则的覆盖请求：谁提的、为什么。

    解析规则（T02）：
    - source 为 ATTACHMENT_TEXT → 一律拒绝（附件是被分析内容，不是指令）；
    - 目标规则为真实系统/权限边界（config.boundary == "system"）→ 一律拒绝；
    - warning 建议 / 普通默认规则 → 用户明确覆盖生效并记录。
    """

    rule_id: str = Field(min_length=1, pattern=r"^[A-Z][0-9A-Z_]*$")
    source: SourceRef
    reason: str = ""


# ---------- 工程规则 ----------


class EngineeringRule(BaseModel):
    """一条版本化工程规则。

    优先级（§3）：真实系统/权限边界 > 用户明确要求 > 用户已确认规则 > 默认建议。
    severity=blocking 的真实权限冲突不能被任何覆盖合并掉。
    """

    rule_id: str = Field(min_length=1, pattern=r"^[A-Z][0-9A-Z_]*$")
    revision: int = Field(ge=1)
    severity: Severity
    applies_to: str = ""      # 适用目标（文件类型/阶段/平台），空=全部
    description: str = Field(min_length=1)
    checker_id: str = ""      # 检查器 ID（空=纯语义规则，人工/模型判断）
    config: dict[str, Any] = Field(default_factory=dict)  # 可配置阈值等


class EngineeringProfile(BaseModel):
    """工程规则集：版本 + ID 唯一 + 可校验。"""

    schema_version: str = SCHEMA_VERSION
    profile_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    rules: list[EngineeringRule] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _unique_rule_ids(cls, v: list[EngineeringRule]) -> list[EngineeringRule]:
        _reject_duplicate_ids([r.rule_id for r in v], "规则")
        return v


# ---------- 工程方案 / 产物清单 ----------


class AcceptanceItem(BaseModel):
    """一个验收项（静态/执行/人工/宿主四类）。"""

    acceptance_id: str = Field(min_length=1)
    kind: Literal["static", "execution", "manual", "host"] = "static"
    description: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)


class PlannedFile(BaseModel):
    """方案中的一个将生成文件（T05 阶段只描述，不声称已存在）。"""

    path: str = Field(min_length=1)
    kind: ArtifactKind = ArtifactKind.TEXT
    purpose: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)


class SkillPlan(BaseModel):
    """工程方案：功能 → 文件 → 验收映射。"""

    schema_version: str = SCHEMA_VERSION
    plan_id: str = Field(min_length=1)
    files: list[PlannedFile] = Field(min_length=1)
    acceptances: list[AcceptanceItem] = Field(min_length=1)
    tool_notes: list[str] = Field(default_factory=list)  # 工具真实可用性标注
    blocking_gaps: list[str] = Field(default_factory=list)  # 无法满足的功能

    @field_validator("files")
    @classmethod
    def _unique_file_paths(cls, v: list[PlannedFile]) -> list[PlannedFile]:
        _reject_duplicate_ids([f.path for f in v], "文件")
        return v

    @field_validator("acceptances")
    @classmethod
    def _unique_acceptance_ids(cls, v: list[AcceptanceItem]) -> list[AcceptanceItem]:
        _reject_duplicate_ids([a.acceptance_id for a in v], "验收")
        return v

    def gaps_for(self, contract: "RequirementContract") -> list[str]:
        """T05 覆盖判定：每项必要功能至少一个实现文件 + 一个验收项。

        返回缺口描述（无缺口 → 空列表）。blocking_gaps 中的功能视为
        已显式声明无法满足，不重复报告。"""
        gap_reqs = {g.split(":")[0].strip() for g in self.blocking_gaps}
        files_by_req: dict[str, int] = {}
        for f in self.files:
            for rid in f.requirement_ids:
                files_by_req[rid] = files_by_req.get(rid, 0) + 1
        accs_by_req: dict[str, int] = {}
        for a in self.acceptances:
            for rid in a.requirement_ids:
                accs_by_req[rid] = accs_by_req.get(rid, 0) + 1

        gaps: list[str] = []
        for req in contract.requirements:
            if not req.necessary or req.requirement_id in gap_reqs:
                continue
            if files_by_req.get(req.requirement_id, 0) == 0:
                gaps.append(f"{req.requirement_id}: 无实现文件")
            elif accs_by_req.get(req.requirement_id, 0) == 0:
                gaps.append(f"{req.requirement_id}: 无验收项")
        return gaps


class ArtifactFile(BaseModel):
    """产物清单中的一个文件：文本带内容，二进制带资源 ID。

    kind=text 必须有 content 且不得有 resource_id；
    kind=binary 必须有 resource_id（已上传资源），不得有 content。
    任意外部下载链接不是可用文件（§3）。
    """

    path: str = Field(min_length=1)
    kind: ArtifactKind
    sha256: str = Field(min_length=1)
    purpose: str = ""
    requirement_ids: list[str] = Field(default_factory=list)
    content: str | None = None
    resource_id: str | None = None

    @model_validator(mode="after")
    def _kind_content_mutex(self) -> "ArtifactFile":
        if self.kind == ArtifactKind.TEXT:
            if self.content is None:
                raise ValueError(f"text 产物缺 content: {self.path}")
            if self.resource_id is not None:
                raise ValueError(f"text 产物不得带 resource_id: {self.path}")
        else:
            if self.resource_id is None:
                raise ValueError(f"binary 产物缺 resource_id: {self.path}")
            if self.content is not None:
                raise ValueError(f"binary 产物不得内联 content: {self.path}")
        return self


class ArtifactManifest(BaseModel):
    """产物清单。"""

    schema_version: str = SCHEMA_VERSION
    artifacts: list[ArtifactFile] = Field(min_length=1)

    @field_validator("artifacts")
    @classmethod
    def _unique_paths(cls, v: list[ArtifactFile]) -> list[ArtifactFile]:
        _reject_duplicate_ids([a.path for a in v], "产物")
        return v


# ---------- 检查报告 ----------


class CheckResult(BaseModel):
    """单项检查结果：证据 + 产物哈希，模型自评不能伪造。"""

    check_id: str = Field(min_length=1)
    status: CheckStatus
    evidence: str = ""       # 证据说明（运行输出/断言结果/未运行原因）
    artifact_hash: str = ""  # 检查对象哈希（空=非产物级检查）


class CheckReport(BaseModel):
    """检查报告：一个阶段或整体的检查集合。"""

    schema_version: str = SCHEMA_VERSION
    checks: list[CheckResult] = Field(default_factory=list)

    @field_validator("checks")
    @classmethod
    def _unique_check_ids(cls, v: list[CheckResult]) -> list[CheckResult]:
        _reject_duplicate_ids([c.check_id for c in v], "检查")
        return v

    @property
    def has_failed(self) -> bool:
        """存在 failed 状态（调用方按规则 severity 判定是否阻塞）。"""
        return any(c.status == CheckStatus.FAILED for c in self.checks)


# ---------- 预算 / 快照 / 收据 ----------


class FlowBudget(BaseModel):
    """全流程预算：跨生成/评测/修复累计（T11 接管记账，此处定义结构）。"""

    schema_version: str = SCHEMA_VERSION
    total_tokens: int = Field(ge=0, default=200_000)
    max_tokens_total: int | None = Field(default=None, ge=0)
    max_model_calls: int | None = Field(default=None, ge=0)
    max_tool_seconds: int | None = Field(default=None, ge=0)
    max_repair_rounds: int | None = Field(default=3, ge=0)
    deadline_seconds: float | None = Field(default=None, ge=0)
    used_tokens: int = Field(default=0, ge=0)
    used_model_calls: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    unknown_usage_calls: int = Field(default=0, ge=0)
    used_tool_seconds: float = Field(default=0.0, ge=0)
    used_repair_rounds: int = Field(default=0, ge=0)


class FlowSnapshot(BaseModel):
    """流程快照：状态机 + 乐观并发（expected_revision，冲突 409）。"""

    schema_version: str = SCHEMA_VERSION
    flow_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    state: FlowState = FlowState.QUEUED
    revision: int = Field(ge=1, default=1)  # CAS 版本号
    contract: RequirementContract | None = None
    profile_snapshot: EngineeringProfile | None = None
    plan: SkillPlan | None = None
    candidate_manifest: ArtifactManifest | None = None
    latest_report: CheckReport | None = None
    budget: FlowBudget = Field(default_factory=FlowBudget)
    checkpoint: dict[str, Any] = Field(default_factory=dict)  # 阶段检查点
    pending_questions: list[str] = Field(default_factory=list)  # 必要缺口问题
    error: str | None = None


class DeliveryReceipt(BaseModel):
    """交付收据：五维状态分离（§2），未验证候选必须与已交付区分。"""

    schema_version: str = SCHEMA_VERSION
    flow_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    delivery_status: DeliveryStatus
    static_status: CheckStatus | None = None
    execution_status: CheckStatus | None = None
    coverage_status: CheckStatus | None = None
    host_status: CheckStatus | None = None
    trigger_status: CheckStatus | None = None  # v1.4 R1：第六维（触发质量，导出时刻折算）
    blind_status: CheckStatus | None = None  # v1.4 R3：盲评偏好（建议性，导出时刻折算）
    zip_path: str = ""
    requirement_coverage: list[CheckResult] = Field(default_factory=list)
    evaluation_usage: list[dict[str, Any]] = Field(default_factory=list)
    generation_budget_ledger: dict[str, dict[str, Any]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)  # 未验证项/缺口说明

    @property
    def fully_verified(self) -> bool:
        """只有全部必要维度 passed 且 delivery_status=delivered 才算满足约定验收。"""
        dims = (self.static_status, self.execution_status, self.coverage_status, self.host_status)
        return self.delivery_status == DeliveryStatus.DELIVERED and all(
            d == CheckStatus.PASSED for d in dims
        )
