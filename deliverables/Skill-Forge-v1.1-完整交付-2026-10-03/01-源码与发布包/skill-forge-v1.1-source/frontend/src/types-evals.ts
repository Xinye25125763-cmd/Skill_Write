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
