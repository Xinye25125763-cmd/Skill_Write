/** 生成流程类型：严格映射 FlowSnapshot 与 DeliveryReceipt（T17）。 */

export interface FlowBudgetView {
  total_tokens?: number;
  used_tokens: number;
  used_model_calls: number;
  estimated_tokens?: number;
  unknown_usage_calls?: number;
  used_tool_seconds?: number;
  used_repair_rounds?: number;
}

export interface RequirementView {
  requirement_id: string;
  content: string;
  necessary: boolean;
  source_type?: string;
  confirmation?: string;
}

export interface EngineeringProfileView {
  profile_id: string;
  revision: number;
  rules: { rule_id: string; [k: string]: unknown }[];
}

export interface CheckResultView {
  check_id: string;
  status: "passed" | "failed" | "not_run" | "unsupported";
  evidence: string;
  artifact_hash?: string;
}

export interface DeliveryReceiptView {
  flow_id: string;
  version_id: string;
  delivery_status: "candidate" | "delivered";
  static_status: string | null;
  execution_status: string | null;
  coverage_status: string | null;
  host_status: string | null;
  zip_path: string;
  requirement_coverage: CheckResultView[];
  generation_budget_ledger?: Record<string, Record<string, unknown>>;
  evaluation_usage?: Array<{
    eval_run_id: string;
    kind: "contrast" | "trigger";
    budget: Record<string, unknown>;
    cost: Record<string, number>;
  }>;
  notes: string[];
}

/** GET /api/generation-flows/{id} 视图（FlowSnapshot 投影）。 */
export interface GenerationFlowView {
  flow_id: string;
  project_id: string;
  state: string;
  revision: number;
  budget: FlowBudgetView;
  checkpoint: {
    receipt?: DeliveryReceiptView;
    [k: string]: unknown;
  };
  pending_questions: string[];
  error: string | null;
  job_id: string | null;
  /** 快照携带的合同/规则（awaiting_input 起可见）。 */
  contract?: { requirements: RequirementView[] } | null;
  profile?: EngineeringProfileView | null;
}

export const FLOW_STATES = [
  "queued",
  "extracting",
  "awaiting_input",
  "planning",
  "generating",
  "validating",
  "evaluating",
  "repairing",
  "host_checking",
  "packaging",
  "delivered",
  "failed",
  "cancelled",
  "interrupted",
] as const;

export type FlowState = (typeof FLOW_STATES)[number];

export const TERMINAL_STATES: readonly string[] = ["delivered", "failed", "cancelled"];

export const STATE_LABEL: Record<string, string> = {
  queued: "排队中",
  extracting: "需求提取中",
  awaiting_input: "等待补充（必要问题）",
  planning: "工程规划中",
  generating: "技能生成中",
  validating: "静态验证中",
  evaluating: "对照评测中",
  repairing: "自动修复中",
  host_checking: "宿主验证中",
  packaging: "打包中",
  delivered: "候选已生成（验收状态见收据）",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断（可恢复）",
};

export const CHECK_STATUS_LABEL: Record<string, string> = {
  passed: "通过",
  failed: "未通过",
  not_run: "未运行",
  unsupported: "不支持（环境缺失）",
};

export function isTerminal(state: string): boolean {
  return TERMINAL_STATES.includes(state);
}
