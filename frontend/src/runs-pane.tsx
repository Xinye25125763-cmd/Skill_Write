import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.ts";
import type { AgentRunInfo, JobView } from "./types-runs.ts";
import type { SkillVersionInfo } from "./types-skills.ts";

const EXIT_REASON_LABEL: Record<string, string> = {
  completed: "完成",
  truncated: "输出截断",
  budget_exceeded: "预算耗尽",
  tool_timeout: "工具超时",
  model_failed: "模型失败",
  cancelled: "已取消",
};

/** 运行面板：提交 agent 运行（可选关联版本）→ 轮询 job → 展示轨迹。 */
export function RunsPane({ projectId }: { projectId: string }) {
  const [versions, setVersions] = useState<SkillVersionInfo[] | null>(null);
  const [runs, setRuns] = useState<AgentRunInfo[] | null>(null);
  const [prompt, setPrompt] = useState("");
  const [versionId, setVersionId] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [selected, setSelected] = useState<AgentRunInfo | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (pollTimer.current) clearTimeout(pollTimer.current);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const vs = await api<SkillVersionInfo[]>(`/api/projects/${projectId}/skills/versions`);
      setVersions(vs);
      setRuns(await api<AgentRunInfo[]>(`/api/projects/${projectId}/runs`));
    } catch (err) {
      setMessage({ kind: "err", text: String(err) });
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const waitForJob = (jobId: string, maxAttempts = 600): Promise<JobView> =>
    new Promise((resolve, reject) => {
      const tick = async (n: number) => {
        try {
          const job = await api<JobView>(`/api/jobs/${jobId}`);
          if (["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) {
            resolve(job);
            return;
          }
          if (n > maxAttempts) {
            reject(new Error(`任务 ${jobId} 长时间未完成（${job.status}）`));
            return;
          }
          pollTimer.current = setTimeout(() => void tick(n + 1), 500);
        } catch (err) {
          reject(err);
        }
      };
      void tick(0);
    });

  const submit = async () => {
    if (!prompt.trim()) return;
    setRunning(true);
    setSelected(null);
    setMessage({ kind: "ok", text: "已提交运行任务，等待 Worker 执行…" });
    try {
      const body: Record<string, unknown> = { prompt };
      if (versionId) body.version_id = versionId;
      const resp = await api<{ job_id: string }>(`/api/projects/${projectId}/runs`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      const job = await waitForJob(resp.job_id);
      if (job.status === "succeeded" && job.result) {
        const run = job.result;
        setSelected(run);
        const label = EXIT_REASON_LABEL[run.exit_reason ?? ""] ?? run.exit_reason;
        setMessage({
          kind: run.exit_reason === "completed" ? "ok" : "err",
          text: `运行结束：${label}（${run.usage.prompt_tokens + run.usage.completion_tokens} tokens，${run.steps.length} 步）${run.exit_reason !== "completed" && run.error_detail ? ` — ${run.error_detail}` : ""}`,
        });
      } else {
        const err = job.error ?? null;
        setMessage({
          kind: "err",
          text: `运行失败（${err?.code ?? job.status}）：${err?.message ?? ""}`,
        });
      }
      await refresh();
    } catch (err) {
      setMessage({ kind: "err", text: `运行失败：${err}` });
    } finally {
      setRunning(false);
    }
  };

  return (
    <section>
      <h2>运行（API 执行器）</h2>
      {message && (
        <p style={{ color: message.kind === "ok" ? "#2a7" : "#c33", whiteSpace: "pre-wrap" }}>
          {message.text}
        </p>
      )}
      <div style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "flex-start" }}>
        <textarea
          value={prompt}
          placeholder="任务提示词（会交给模型在沙箱中执行）"
          onChange={(e) => setPrompt(e.target.value)}
          rows={3}
          style={{ flex: 1, fontFamily: "inherit", fontSize: 13, padding: "6px 8px" }}
        />
        <div style={{ width: 200, flexShrink: 0 }}>
          <select
            value={versionId}
            onChange={(e) => setVersionId(e.target.value)}
            style={{ width: "100%", padding: "6px", marginBottom: 8 }}
          >
            <option value="">无技能基线</option>
            {versions?.map((v) => (
              <option key={v.id} value={v.id}>
                {v.label ?? v.id.slice(-6)}（{v.content_hash.slice(0, 8)}…）
              </option>
            ))}
          </select>
          <button onClick={() => void submit()} disabled={running || !prompt.trim()} style={{ width: "100%", padding: "6px" }}>
            {running ? "运行中…" : "提交运行"}
          </button>
        </div>
      </div>

      {selected && <RunDetail run={selected} />}

      <h4>历史运行</h4>
      {runs?.length === 0 && <p style={{ color: "#aaa" }}>尚无运行记录。需要配置 SKILLFORGE_LLM_* 环境变量后由 Worker 执行。</p>}
      <ul style={{ listStyle: "none", padding: 0, fontSize: 13 }}>
        {runs?.map((r) => (
          <li key={r.id} style={{ padding: "6px 0", borderBottom: "1px solid #eee" }}>
            <button
              style={{ border: "none", background: "none", cursor: "pointer", color: "#06c", padding: 0, textAlign: "left" }}
              onClick={() => setSelected(r)}
            >
              {r.prompt.slice(0, 50)}
              {r.prompt.length > 50 ? "…" : ""}
            </button>
            <span style={{ color: "#888" }}>
              {" "}· {r.version_id ? `版本 ${r.version_id.slice(-6)}` : "基线"} ·{" "}
              {EXIT_REASON_LABEL[r.exit_reason ?? ""] ?? r.exit_reason ?? r.status} ·{" "}
              {r.usage.prompt_tokens + r.usage.completion_tokens} tokens
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RunDetail({ run }: { run: AgentRunInfo }) {
  return (
    <div
      style={{
        border: "1px solid #ddd",
        borderRadius: 6,
        padding: 12,
        marginBottom: 16,
        background: "#fafafa",
      }}
    >
      <div style={{ marginBottom: 8 }}>
        <b>运行 {run.id.slice(-6)}</b>
        <span style={{ color: "#888" }}>
          {" "}· {EXIT_REASON_LABEL[run.exit_reason ?? ""] ?? run.exit_reason} · {run.steps.length} 步 ·{" "}
          {run.usage.prompt_tokens + run.usage.completion_tokens} tokens
        </span>
      </div>
      {run.budget.limit_tokens !== undefined && (
        <p style={{ color: run.budget.over_limit ? "#a33" : "#555", fontSize: 13 }}>
          预算：{run.budget.used_tokens} / {run.budget.limit_tokens} tokens
          {run.budget.over_limit ? `，超额 ${run.budget.overrun_tokens} tokens` : ""}
          {run.budget.over_limit && run.exit_reason === "completed" ? "；输出已完成，预算已超额" : ""}
        </p>
      )}
      {run.final_message && (
        <p style={{ whiteSpace: "pre-wrap", background: "#fff", padding: 8, borderRadius: 4, border: "1px solid #eee" }}>
          {run.final_message}
        </p>
      )}
      <details>
        <summary style={{ cursor: "pointer" }}>轨迹（{run.steps.length} 步）</summary>
        <ol style={{ fontSize: 12 }}>
          {run.steps.map((s, i) => (
            <li key={i} style={{ marginBottom: 6 }}>
              <b>{s.kind}</b>
              {s.name ? `:${s.name}` : ""}
              {s.duration_ms != null && <span style={{ color: "#888" }}> · {s.duration_ms}ms</span>}
              {s.usage && (
                <span style={{ color: "#888" }}>
                  {" "}· {s.usage.prompt_tokens}+{s.usage.completion_tokens} tok
                </span>
              )}
              {s.arguments && (
                <pre style={{ margin: "4px 0", background: "#fff", padding: 4, borderRadius: 4, overflowX: "auto" }}>
                  {JSON.stringify(s.arguments, null, 1)}
                </pre>
              )}
              {s.output && (
                <pre style={{ margin: "4px 0", color: "#555", background: "#fff", padding: 4, borderRadius: 4, overflowX: "auto", maxHeight: 200 }}>
                  {s.output}
                </pre>
              )}
            </li>
          ))}
        </ol>
      </details>
      <details>
        <summary style={{ cursor: "pointer" }}>沙箱文件（{run.sandbox_files.files.length} 个）</summary>
        <ul style={{ fontSize: 12 }}>
          {run.sandbox_files.files.map((f) => (
            <li key={f.path}>
              <code>{f.path}</code>{" "}
              <span style={{ color: "#888" }}>
                {f.text ? `${f.size} B` : `二进制 · ${f.size} B`}
              </span>
              {f.text && run.sandbox_files.text_contents[f.path] !== undefined && (
                <pre style={{ margin: "2px 0", background: "#fff", padding: 4, borderRadius: 4, maxHeight: 160, overflow: "auto" }}>
                  {run.sandbox_files.text_contents[f.path]}
                </pre>
              )}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
