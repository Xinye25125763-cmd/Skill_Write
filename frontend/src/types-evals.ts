/** S6 类型：测试集与对照评测。 */

export interface AssertionSpec {
  type: "file_exists" | "file_contains" | "text_contains" | "exit_reason_is";
  path?: string | null;
  contains?: string | null;
  value?: string | null;
}

export interface CaseSpec {
  id: string;
  name?: string | null;
  prompt: string;
  assertions: AssertionSpec[];
}

export interface SuiteInfo {
  id: string;
  project_id: string;
  name: string;
  created_at: number;
  cases?: CaseSpec[];
}

export interface AssertionResult {
  type: string;
  description: string;
  passed: boolean;
  detail: string | null;
}

export interface CaseResultView {
  id: string;
  case_id: string;
  group: "candidate" | "baseline";
  agent_run_id: string | null;
  exec_status: "executed" | "exec_failed" | "skipped";
  exit_reason: string | null;
  score_status: "scored" | "score_failed" | "not_scored";
  assertions: AssertionResult[];
  passed_count: number | null;
  total_count: number | null;
  score_error: string | null;
}

export interface GroupSummary {
  cases: number;
  executed: number;
  exec_failed: number;
  skipped: number;
  scored: number;
  score_failed: number;
  passed_total: number;
  assertions_total: number;
}

export interface EvalRunView {
  id: string;
  project_id: string;
  suite_id: string;
  version_id: string | null;
  status: "running" | "finished";
  error: { code?: string; message?: string } | null;
  summary: Record<"candidate" | "baseline", GroupSummary>;
  cost?: { total_tokens: number; candidate_tokens: number; baseline_tokens: number; judge_tokens: number };
  budget?: {
    per_run_limit_override: number | null;
    per_run_limits_by_case: Record<string, number | null>;
    suite_total_cap: number | null;
    used_tokens: number;
    cap_hit: boolean;
    cap_accounted_tokens?: number;
    unknown_usage_estimated_tokens?: number;
    usage_missing_calls?: number;
    suite_cap_scope: string;
    judge_tokens_excluded: number | null;
  } | null;
  blind?: {
    usage_missing_calls?: number;
    usage_calls?: number;
    skipped?: string;
    budget_ledger_error?: string;
  } | null;
  case_results: CaseResultView[];
  created_at: number;
  finished_at: number | null;
}

/** eval_run 的 job 视图（result 为 EvalRunView）。 */
export interface EvalJobView {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  result: EvalRunView | null;
  error: { code?: string; message?: string; errors?: string[] } | null;
}
