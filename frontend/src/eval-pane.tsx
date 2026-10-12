import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.ts";
import type { CaseResultView, EvalJobView, EvalRunView, SuiteInfo } from "./types-evals.ts";
import type { SkillVersionInfo } from "./types-skills.ts";

const EXEC_STATUS_LABEL: Record<string, string> = {
  executed: "已执行",
  exec_failed: "执行失败",
  skipped: "已跳过",
};

const SCORE_STATUS_LABEL: Record<string, string> = {
  scored: "已评分",
  score_failed: "评分失败",
  not_scored: "未评分",
};

const EXIT_REASON_LABEL: Record<string, string> = {
  completed: "完成",
  truncated: "输出截断",
  budget_exceeded: "预算耗尽",
  tool_timeout: "工具超时",
  model_failed: "模型失败",
  cancelled: "已取消",
};

const STATUS_COLOR: Record<string, string> = {
  executed: "#2a7",
  exec_failed: "#c33",
  skipped: "#999",
  scored: "#2a7",
  score_failed: "#c33",
  not_scored: "#999",
};

const GROUP_LABEL: Record<string, string> = {
  candidate: "候选（带技能）",
  baseline: "基线（无技能）",
};

const SAMPLE_CASES_JSON = JSON.stringify(
  [
    {
      id: "case-1",
      name: "示例用例",
      prompt: "任务提示词……",
      assertions: [
        { type: "exit_reason_is", value: "completed" },
        { type: "file_exists", path: "out/result.md" },
        { type: "file_contains", path: "out/result.md", contains: "关键词" },
        { type: "text_contains", contains: "关键词" },
      ],
    },
  ],
  null,
  2,
);

/** 评测面板：测试集管理 + 对照评测（候选 vs 基线）→ 逐例结果（执行/评分分离展示）。 */
export function EvalPane({ projectId }: { projectId: string }) {
  const [suites, setSuites] = useState<SuiteInfo[] | null>(null);
  const [versions, setVersions] = useState<SkillVersionInfo[] | null>(null);
  const [evalRuns, setEvalRuns] = useState<EvalRunView[] | null>(null);
  const [selected, setSelected] = useState<EvalRunView | null>(null);

  // 创建测试集表单
  const [suiteName, setSuiteName] = useState("");
  const [casesJson, setCasesJson] = useState(SAMPLE_CASES_JSON);
  const [creating, setCreating] = useState(false);
  const [formMsg, setFormMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // 发起评测
  const [suiteId, setSuiteId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [maxTokensPerRun, setMaxTokensPerRun] = useState("");
  const [suiteMaxTokensTotal, setSuiteMaxTokensTotal] = useState("");
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (pollTimer.current) clearTimeout(pollTimer.current);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setSuites(await api<SuiteInfo[]>(`/api/projects/${projectId}/suites`));
      setVersions(await api<SkillVersionInfo[]>(`/api/projects/${projectId}/skills/versions`));
      setEvalRuns(await api<EvalRunView[]>(`/api/projects/${projectId}/eval-runs`));
    } catch (err) {
      setFormMsg({ kind: "err", text: String(err) });
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const waitForJob = (jobId: string, maxAttempts = 1800): Promise<EvalJobView> =>
    new Promise((resolve, reject) => {
      const tick = async (n: number) => {
        try {
          const job = await api<EvalJobView>(`/api/jobs/${jobId}`);
          if (["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) {
            resolve(job);
            return;
          }
          if (n > maxAttempts) {
            reject(new Error(`评测任务 ${jobId} 长时间未完成（${job.status}）`));
            return;
          }
          pollTimer.current = setTimeout(() => void tick(n + 1), 500);
        } catch (err) {
          reject(err);
        }
      };
      void tick(0);
    });

  const createSuite = async () => {
    setCreating(true);
    setFormMsg(null);
    try {
      let cases: unknown;
      try {
        cases = JSON.parse(casesJson);
      } catch {
        throw new Error("用例 JSON 解析失败，请检查格式");
      }
      const suite = await api<SuiteInfo>(`/api/projects/${projectId}/suites`, {
        method: "POST",
        body: JSON.stringify({ name: suiteName.trim() || "未命名测试集", cases }),
      });
      setFormMsg({ kind: "ok", text: `测试集已创建：${suite.id.slice(-6)}` });
      setSuiteName("");
      await refresh();
    } catch (err) {
      setFormMsg({ kind: "err", text: `创建失败：${err}` });
    } finally {
      setCreating(false);
    }
  };

  const submitEval = async () => {
    if (!suiteId || !versionId) return;
    setRunning(true);
    setSelected(null);
    setRunMsg({ kind: "ok", text: "已入队对照评测，等待 Worker 执行…" });
    try {
      const resp = await api<{ job_id: string }>(`/api/projects/${projectId}/eval-runs`, {
        method: "POST",
        body: JSON.stringify({
          suite_id: suiteId,
          version_id: versionId,
          ...(maxTokensPerRun.trim() ? { max_tokens_per_run: Number(maxTokensPerRun) } : {}),
          ...(suiteMaxTokensTotal.trim() ? { max_tokens_total: Number(suiteMaxTokensTotal) } : {}),
        }),
      });
      const job = await waitForJob(resp.job_id);
      if (job.status === "succeeded" && job.result) {
        setSelected(job.result);
        setRunMsg({ kind: "ok", text: "评测完成，结果如下。" });
      } else {
        const err = job.error ?? null;
        setRunMsg({
          kind: "err",
          text: `评测失败（${err?.code ?? job.status}）：${err?.message ?? ""}`,
        });
      }
      await refresh();
    } catch (err) {
      setRunMsg({ kind: "err", text: `评测失败：${err}` });
    } finally {
      setRunning(false);
    }
  };

  return (
    <section>
      <h2>评测（候选 vs 基线）</h2>

      {/* 创建测试集 */}
      <details style={{ marginBottom: 16 }}>
        <summary style={{ cursor: "pointer" }}>创建测试集</summary>
        <div style={{ marginTop: 8 }}>
          {formMsg && (
            <p style={{ color: formMsg.kind === "ok" ? "#2a7" : "#c33", whiteSpace: "pre-wrap" }}>
              {formMsg.text}
            </p>
          )}
          <input
            value={suiteName}
            placeholder="测试集名称"
            onChange={(e) => setSuiteName(e.target.value)}
            style={{ padding: "6px 8px", width: "100%", marginBottom: 8 }}
          />
          <textarea
            value={casesJson}
            onChange={(e) => setCasesJson(e.target.value)}
            rows={10}
            spellCheck={false}
            style={{
              width: "100%",
              fontFamily: "ui-monospace, monospace",
              fontSize: 12,
              padding: "6px 8px",
            }}
          />
          <p style={{ color: "#888", fontSize: 12, margin: "4px 0 8px" }}>
            用例数组，每项：id（唯一）、prompt、assertions（type 为 file_exists / file_contains /
            text_contains / exit_reason_is）。
          </p>
          <button onClick={() => void createSuite()} disabled={creating} style={{ padding: "6px 16px" }}>
            {creating ? "创建中…" : "创建测试集"}
          </button>
        </div>
      </details>

      {/* 发起评测 */}
      <div style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "center" }}>
        <select
          value={suiteId}
          onChange={(e) => setSuiteId(e.target.value)}
          style={{ flex: 1, padding: "6px" }}
        >
          <option value="">选择测试集…</option>
          {suites?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}（{s.id.slice(-6)}）
            </option>
          ))}
        </select>
        <select
          value={versionId}
          onChange={(e) => setVersionId(e.target.value)}
          style={{ flex: 1, padding: "6px" }}
        >
          <option value="">选择候选版本…</option>
          {versions?.map((v) => (
            <option key={v.id} value={v.id}>
              {v.label ?? v.id.slice(-6)}（{v.content_hash.slice(0, 8)}…）
            </option>
          ))}
        </select>
        <button
          onClick={() => void submitEval()}
          disabled={running || !suiteId || !versionId}
          style={{ padding: "6px 16px" }}
        >
          {running ? "评测中…" : "发起对照评测"}
        </button>
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12, fontSize: 13 }}>
        <label>
          单次运行 token 上限（候选/基线各自，空白使用测试集声明/系统默认）
          <input type="number" min={100} max={10000000} step={1000} value={maxTokensPerRun}
            onChange={(e) => setMaxTokensPerRun(e.target.value)} placeholder="使用声明/默认"
            style={{ display: "block", padding: 6, width: 190 }} />
        </label>
        <label>
          套件累计 token 上限（候选 + 基线，空白使用测试集声明/不限额）
          <input type="number" min={100} max={100000000} step={1000} value={suiteMaxTokensTotal}
            onChange={(e) => setSuiteMaxTokensTotal(e.target.value)} placeholder="使用声明/不限额"
            style={{ display: "block", padding: 6, width: 220 }} />
        </label>
        <p style={{ color: "#777", maxWidth: 420, margin: "18px 0 0" }}>
          明确输入的上限会覆盖测试集声明，既可调高也可调低。盲评评委在候选/基线执行后运行，单独计量，不占用套件累计上限。
        </p>
      </div>

      {runMsg && (
        <p style={{ color: runMsg.kind === "ok" ? "#2a7" : "#c33", whiteSpace: "pre-wrap" }}>
          {runMsg.text}
        </p>
      )}

      {selected && <EvalResultDetail view={selected} />}

      {/* 评测历史 */}
      <h4>评测历史</h4>
      {evalRuns?.length === 0 && (
        <p style={{ color: "#aaa" }}>尚无评测记录。选择测试集与候选版本发起对照评测。</p>
      )}
      <ul style={{ listStyle: "none", padding: 0, fontSize: 13 }}>
        {evalRuns?.map((r) => (
          <li key={r.id} style={{ padding: "6px 0", borderBottom: "1px solid #eee" }}>
            <button
              style={{ border: "none", background: "none", cursor: "pointer", color: "#06c", padding: 0 }}
              onClick={() => setSelected(r)}
            >
              评测 {r.id.slice(-6)}
            </button>
            <span style={{ color: "#888" }}>
              {" "}· 版本 {r.version_id?.slice(-6)} ·{" "}
              候选 {r.summary.candidate.passed_total}/{r.summary.candidate.assertions_total} ·{" "}
              基线 {r.summary.baseline.passed_total}/{r.summary.baseline.assertions_total}
              {r.status !== "finished" && ` · ${r.status}`}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function EvalResultDetail({ view }: { view: EvalRunView }) {
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
        <b>评测 {view.id.slice(-6)}</b>
        <span style={{ color: "#888" }}>
          {" "}· {view.status === "finished" ? "已完成" : view.status} · 套件 {view.suite_id.slice(-6)} · 候选版本{" "}
          {view.version_id?.slice(-6)}
        </span>
      </div>

      <div style={{ margin: "8px 0 12px", padding: 8, background: "#fff", border: "1px solid #e5e5e5", fontSize: 13 }}>
        <b>预算与用量</b>
        <div>
          单次运行上限：{formatBudgetLimits(view.budget?.per_run_limits_by_case)}；套件累计上限：{formatTokens(view.budget?.suite_total_cap)}；
          候选/基线实测用量：{formatTokens(view.budget?.used_tokens ?? ((view.cost?.candidate_tokens ?? 0) + (view.cost?.baseline_tokens ?? 0)))}
          {view.budget?.unknown_usage_estimated_tokens ? `；无 usage 失败调用保守估算 ${formatTokens(view.budget.unknown_usage_estimated_tokens)}` : ""}
          {view.budget?.cap_hit ? "（已触发上限，后续用例已跳过）" : "（未触发上限）"}
        </div>
        <div>
          实际用量明细：候选 {formatTokens(view.cost?.candidate_tokens)} · 基线 {formatTokens(view.cost?.baseline_tokens)} ·
          评委 {formatTokens(view.cost?.judge_tokens)}（单独计量，不计入套件上限）
          {(view.blind?.usage_missing_calls ?? 0) > 0 && `；评委有 ${view.blind?.usage_missing_calls} 次调用未返回 token 用量，费用统计不完整`}
          {view.blind?.budget_ledger_error ? `；评委成本台账写入失败：${view.blind.budget_ledger_error}` : ""}
        </div>
      </div>

      {(["candidate", "baseline"] as const).map((group) => (
        <GroupBlock key={group} group={group} view={view} />
      ))}

      <h4 style={{ marginBottom: 6 }}>逐例结果（执行状态与评分状态分离）</h4>
      <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
        <thead>
          <tr>
            {["用例", "组", "执行状态", "退出原因", "评分状态", "通过", "评分错误"].map((h) => (
              <th
                key={h}
                style={{
                  textAlign: "left",
                  borderBottom: "1px solid #ccc",
                  padding: "4px 8px",
                  background: "#f0f0f0",
                }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {view.case_results.map((c) => (
            <CaseRow key={c.id} c={c} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GroupBlock({ group, view }: { group: "candidate" | "baseline"; view: EvalRunView }) {
  const s = view.summary[group];
  return (
    <div style={{ marginBottom: 8, fontSize: 13 }}>
      <b>{GROUP_LABEL[group]}</b>
      <span style={{ color: "#555" }}>
        {" "}· 断言通过 {s.passed_total}/{s.assertions_total} · 执行 {s.executed} / 执行失败{" "}
        <span style={{ color: s.exec_failed > 0 ? "#c33" : undefined }}>{s.exec_failed}</span> / 跳过{" "}
        {s.skipped} · 评分 {s.scored} / 评分失败{" "}
        <span style={{ color: s.score_failed > 0 ? "#c33" : undefined }}>{s.score_failed}</span>
      </span>
    </div>
  );
}

function CaseRow({ c }: { c: CaseResultView }) {
  return (
    <tr>
      <td style={{ padding: "4px 8px", borderBottom: "1px solid #eee" }}>{c.case_id}</td>
      <td style={{ padding: "4px 8px", borderBottom: "1px solid #eee" }}>
        {c.group === "candidate" ? "候选" : "基线"}
      </td>
      <td
        style={{
          padding: "4px 8px",
          borderBottom: "1px solid #eee",
          color: STATUS_COLOR[c.exec_status],
        }}
      >
        {EXEC_STATUS_LABEL[c.exec_status] ?? c.exec_status}
      </td>
      <td style={{ padding: "4px 8px", borderBottom: "1px solid #eee", color: "#888" }}>
        {c.exit_reason ? (EXIT_REASON_LABEL[c.exit_reason] ?? c.exit_reason) : "—"}
      </td>
      <td
        style={{
          padding: "4px 8px",
          borderBottom: "1px solid #eee",
          color: STATUS_COLOR[c.score_status],
        }}
      >
        {SCORE_STATUS_LABEL[c.score_status] ?? c.score_status}
      </td>
      <td style={{ padding: "4px 8px", borderBottom: "1px solid #eee" }}>
        {c.score_status === "scored" ? `${c.passed_count}/${c.total_count}` : "—"}
      </td>
      <td style={{ padding: "4px 8px", borderBottom: "1px solid #eee", color: "#c33" }}>
        {c.score_error ?? ""}
      </td>
    </tr>
  );
}

function formatTokens(value: number | null | undefined): string {
  return value === null || value === undefined ? "未设置/未知" : `${value.toLocaleString()} tokens`;
}

function formatBudgetLimits(limits: Record<string, number | null> | undefined): string {
  if (!limits || Object.keys(limits).length === 0) return "使用执行器默认";
  const values = [...new Set(Object.values(limits))];
  if (values.length === 1) return formatTokens(values[0]);
  return Object.entries(limits).map(([id, value]) => `${id}: ${formatTokens(value)}`).join("；");
}
