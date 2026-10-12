/** S5 类型：agent 运行记录。 */

export interface StepRecord {
  kind: "model" | "tool";
  name: string | null;
  arguments: Record<string, unknown> | null;
  content: string | null;
  output: string | null;
  usage: { prompt_tokens: number; completion_tokens: number } | null;
  duration_ms: number | null;
}

export interface AgentRunInfo {
  id: string;
  project_id: string;
  version_id: string | null;
  prompt: string;
  status: "running" | "finished";
  exit_reason: string | null;
  final_message: string | null;
  steps: StepRecord[];
  usage: { prompt_tokens: number; completion_tokens: number };
  error_detail: string | null;
  /** F09：清单（含二进制）与文本内容分离 */
  sandbox_files: {
    files: { path: string; size: number; text: boolean }[];
    text_contents: Record<string, string>;
  };
  budget: { limit_tokens?: number; used_tokens?: number; over_limit?: boolean; overrun_tokens?: number };
  sandbox_dir: string | null;
  created_at: number;
  finished_at: number | null;
}

export interface JobView {
  id: string;
  type: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  created_at: number;
  updated_at: number;
  lease_expires_at: number | null;
  cancel_requested: boolean;
  result: AgentRunInfo | null;
  error: { code: string; message?: string; errors?: string[]; existing_version_id?: string } | null;
}
