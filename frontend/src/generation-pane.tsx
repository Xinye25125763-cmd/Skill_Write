import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.ts";
import {
  CHECK_STATUS_LABEL,
  STATE_LABEL,
  isTerminal,
} from "./types-generation.ts";
import type {
  CheckResultView,
  DeliveryReceiptView,
  GenerationFlowView,
} from "./types-generation.ts";

const FLOW_KEY = "skillforge_generation_flow";

/** 五维状态：候选 / 静态 / 运行（执行+对照）/ 覆盖 / 宿主——分列如实呈现。 */
function DimensionRow({ label, status }: { label: string; status: string | null }) {
  const text = status === null ? "未接入（not run）" : (CHECK_STATUS_LABEL[status] ?? status);
  const color =
    status === "passed" ? "#2a7" :
    status === "failed" ? "#c33" :
    status === null || status === "not_run" ? "#c80" : "#888";
  return (
    <tr>
      <td style={{ padding: "4px 12px 4px 0" }}>{label}</td>
      <td style={{ padding: "4px 0", color, fontWeight: 600 }}>{text}</td>
    </tr>
  );
}

function receiptView(flow: GenerationFlowView): DeliveryReceiptView | null {
  const r = flow.checkpoint?.receipt;
  return r && typeof r === "object" && "delivery_status" in r ? (r as DeliveryReceiptView) : null;
}

/** 生成面板：自然语言入口 → 阶段/证据呈现 → 必要问题 → 交付收据 + 导出。 */
export function GenerationPane({ projectId, pollMs = 1500 }: {
  projectId: string;
  pollMs?: number;
}) {
  const [requestText, setRequestText] = useState("");
  const [flow, setFlow] = useState<GenerationFlowView | null>(null);
  const [receipt, setReceipt] = useState<DeliveryReceiptView | null>(null);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");
  const [exportState, setExportState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const pollFailuresRef = useRef(0); // 连续轮询失败次数（指数退避用）
  const MAX_POLL_BACKOFF_MS = 30_000;

  // 恢复：flow_id 存 localStorage，刷新/重挂载后拉取快照继续显示（验收：刷新可恢复）
  useEffect(() => {
    const saved = localStorage.getItem(`${FLOW_KEY}:${projectId}`);
    if (!saved) return;
    let active = true;
    (async () => {
      try {
        const snap = await api<GenerationFlowView>(`/api/generation-flows/${saved}`);
        if (!active) return;
        setFlow(snap);
        const r = receiptView(snap);
        if (r) setReceipt(r);
        if (!isTerminal(snap.state)) schedulePoll(snap.flow_id);
      } catch {
        /* 恢复失败（流程已删除/网络故障）：清除 stale flow_id，
           保持入口态；用户可重新发起，不再每次刷新重放 404 */
        localStorage.removeItem(`${FLOW_KEY}:${projectId}`);
      }
    })();
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const schedulePoll = useCallback((flowId: string, delayMs?: number) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      if (!mountedRef.current) return;
      try {
        const snap = await api<GenerationFlowView>(`/api/generation-flows/${flowId}`);
        if (!mountedRef.current) return;
        pollFailuresRef.current = 0; // 恢复即重置退避
        setFlow(snap);
        const r = receiptView(snap);
        if (r) setReceipt(r);
        // 终态停止轮询；等待结束绝不自动取消后台（验收 6）
        if (!isTerminal(snap.state)) schedulePoll(flowId);
      } catch (err) {
        if (!mountedRef.current) return;
        setError(String(err));
        // 指数退避重试：连续失败逐次拉长间隔（pollMs→2×→4×…，封顶 30s），
        // 串行等待而非并发风暴；恢复后回到正常间隔
        pollFailuresRef.current = Math.min(pollFailuresRef.current + 1, 20);
        const backoff = Math.min(
          pollMs * 2 ** (pollFailuresRef.current - 1),
          MAX_POLL_BACKOFF_MS,
        );
        schedulePoll(flowId, backoff);
      }
    }, delayMs ?? pollMs);
  }, [pollMs]);

  const start = async () => {
    if (!requestText.trim()) return;
    setError("");
    pollFailuresRef.current = 0; // 新流程重置退避
    try {
      const snap = await api<GenerationFlowView>(
        `/api/projects/${projectId}/generation-flows`,
        { method: "POST", body: JSON.stringify({ request_text: requestText }) },
      );
      localStorage.setItem(`${FLOW_KEY}:${projectId}`, snap.flow_id);
      setFlow(snap);
      setReceipt(null);
      schedulePoll(snap.flow_id);
    } catch (err) {
      setError(String(err));
    }
  };

  const submitAnswer = async () => {
    if (!flow || !answer.trim()) return;
    setError("");
    try {
      await api(`/api/generation-flows/${flow.flow_id}/answers`, {
        method: "POST",
        body: JSON.stringify({
          revision: flow.revision,
          idempotency_key: `ui-${flow.flow_id}-${flow.revision}-${Date.now()}`,
          answers: { answer: answer },
        }),
      });
      setAnswer("");
      schedulePoll(flow.flow_id);
    } catch (err) {
      setError(String(err)); // 409 等 CAS 错误如实显示（detail/error 兼容在 api.ts）
    }
  };

  const cancel = async () => {
    if (!flow) return;
    setError("");
    try {
      const snap = await api<GenerationFlowView>(
        `/api/generation-flows/${flow.flow_id}/cancel`,
        { method: "POST", body: JSON.stringify({ revision: flow.revision, idempotency_key: `cancel-${flow.flow_id}-${flow.revision}` }) },
      );
      setFlow(snap); // 真实终态反馈，不本地臆造
    } catch (err) {
      setError(String(err));
    }
  };

  const exportZip = async () => {
    if (!receipt) return;
    setExportState("busy");
    setError("");
    try {
      const r = await api<DeliveryReceiptView>(
        `/api/projects/${projectId}/skill-versions/${receipt.version_id}/export`,
        { method: "POST" },
      );
      setReceipt(r);
      setExportState("done");
    } catch (err) {
      setExportState("error");
      setError(String(err));
    }
  };

  const stateLabel = flow ? (STATE_LABEL[flow.state] ?? flow.state) : null;

  return (
    <section>
      <h2>自然语言生成</h2>
      <p style={{ color: "#888" }}>
        直接用一句话描述你想要的技能；系统只追问必要问题，完成后给出可下载的交付包。
      </p>

      {!flow && (
        <div>
          <textarea
            placeholder="自然语言描述你要的技能，例如：把会议纪要整理成行动项列表…"
            value={requestText}
            rows={4}
            onChange={(e) => setRequestText(e.target.value)}
            style={{ width: "100%", fontFamily: "monospace", fontSize: 13 }}
          />
          <button
            style={{ marginTop: 8 }}
            disabled={!requestText.trim()}
            onClick={() => void start()}
          >
            开始生成
          </button>
        </div>
      )}

      {error && <p style={{ color: "#c33" }}>{error}</p>}

      {flow && (
        <div>
          <h3>
            阶段：<span data-testid="flow-state">{stateLabel}（{flow.state}）</span>
          </h3>

          {/* 功能清单（合同需求）——阶段证据 */}
          {flow.contract?.requirements?.length ? (
            <div style={{ margin: "12px 0" }}>
              <b>功能清单：</b>
              <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
                {flow.contract.requirements.map((r) => (
                  <li key={r.requirement_id}>
                    <code>{r.requirement_id}</code> {r.content}
                    {r.necessary ? "" : "（非必要）"}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {/* 工程规则版本 */}
          {flow.profile?.rules?.length ? (
            <p style={{ color: "#666" }}>
              工程规则 <code>{flow.profile.profile_id}</code> r{flow.profile.revision}：
              {flow.profile.rules.map((r) => r.rule_id).join("、")}
            </p>
          ) : null}

          {/* 费用（预算台账证据） */}
          {flow.budget ? (
            <p style={{ color: "#666" }}>
              已用：{flow.budget.used_tokens} tokens · {flow.budget.used_model_calls} 次模型调用
              {flow.budget.estimated_tokens ? ` · 估算 ${flow.budget.estimated_tokens} tokens` : ""}
              {flow.budget.unknown_usage_calls ? ` · ${flow.budget.unknown_usage_calls} 次调用 usage 未知` : ""}
            </p>
          ) : null}

          {/* 失败原因 */}
          {flow.error && (
            <p style={{ color: "#c33" }}>
              <b>失败原因：</b>
              {flow.error}
            </p>
          )}

          {/* 只问必要问题 */}
          {flow.state === "awaiting_input" && flow.pending_questions.length > 0 && (
            <div style={{ border: "1px solid #e8c26a", padding: 12, borderRadius: 6, margin: "12px 0" }}>
              <b>必要问题（仅这些必须回答）：</b>
              <ol style={{ margin: "6px 0", paddingLeft: 22 }}>
                {flow.pending_questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ol>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  placeholder="你的回答"
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  style={{ padding: "6px 10px", flex: 1 }}
                />
                <button disabled={!answer.trim()} onClick={() => void submitAnswer()}>
                  提交回答
                </button>
              </div>
            </div>
          )}

          {/* 取消（非终态才可用） */}
          {!isTerminal(flow.state) && flow.state !== "awaiting_input" && (
            <button style={{ margin: "8px 0" }} onClick={() => void cancel()}>
              取消
            </button>
          )}

          {/* 交付收据：五维状态分列，候选/已交付如实区分 */}
          {receipt && (
            <div style={{ border: "1px solid #ddd", padding: 16, borderRadius: 6, marginTop: 16 }}>
              <h4 style={{ marginTop: 0 }}>
                交付收据 ·{" "}
                <span style={{ color: receipt.delivery_status === "delivered" ? "#2a7" : "#c80" }}>
                  {receipt.delivery_status === "delivered"
                    ? "已交付（delivered）"
                    : "未验证候选（candidate）——不得当作验收通过"}
                </span>
              </h4>
              <table style={{ borderCollapse: "collapse", fontSize: 14 }}>
                <tbody>
                  <DimensionRow label="静态验证" status={receipt.static_status} />
                  <DimensionRow label="运行验证（执行/对照）" status={receipt.execution_status} />
                  <DimensionRow label="需求覆盖" status={receipt.coverage_status} />
                  <DimensionRow label="宿主验证" status={receipt.host_status} />
                </tbody>
              </table>
              {receipt.notes.length > 0 && (
                <ul style={{ color: "#a60", paddingLeft: 20, margin: "8px 0" }}>
                  {receipt.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              )}
              {Object.keys(receipt.generation_budget_ledger ?? {}).length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary>生成阶段模型用量（含重试）</summary>
                  <ul style={{ paddingLeft: 20 }}>
                    {Object.entries(receipt.generation_budget_ledger ?? {}).map(([key, entry]) => {
                      const actual = (entry.actual ?? {}) as Record<string, unknown>;
                      const known = Number(actual.prompt_tokens ?? 0) + Number(actual.completion_tokens ?? 0);
                      return <li key={key}>
                        {String(entry.stage_id ?? key)} / {String(entry.attempt_id ?? "attempt")}：
                        {String(actual.model_calls ?? "未知")} 次调用；实测 {formatReceiptTokens(known)}；
                        估算 {formatReceiptTokens(actual.unknown_usage_estimated_tokens ?? (actual.estimated ? entry.reserved : 0))}
                        {actual.usage_unknown ? "；usage 未知" : ""}
                      </li>;
                    })}
                  </ul>
                </details>
              )}
              {(receipt.evaluation_usage?.length ?? 0) > 0 ? (
                <details style={{ marginTop: 8 }} open>
                  <summary>评测预算与用量</summary>
                  <ul style={{ paddingLeft: 20 }}>
                    {receipt.evaluation_usage?.map((item) => (
                      <li key={item.eval_run_id}>
                        {item.kind === "contrast" ? "对照评测" : "触发验证"}：
                        {item.kind === "contrast" ? (
                          <>单次上限 {formatReceiptLimit(item.budget.per_run_limits_by_case)}；整套上限 {formatReceiptTokens(item.budget.suite_total_cap)}；
                            实测 {formatReceiptTokens(item.budget.actual_tokens)}；估算 {formatReceiptTokens(item.budget.unknown_usage_estimated_tokens)}；
                            {item.budget.cap_hit ? "已触顶" : "未触顶"}；候选 {formatReceiptTokens(item.cost.candidate_tokens)}、基线 {formatReceiptTokens(item.cost.baseline_tokens)}、评委 {formatReceiptTokens(item.cost.judge_tokens)}（评委单独计量，未知用量估算 {formatReceiptTokens(item.budget.judge_unknown_usage_estimated_tokens)}）
                            {item.budget.budget_ledger_error ? `；成本台账写入错误：${item.budget.budget_ledger_error}` : ""}
                            {Number(item.cost.judge_usage_missing_calls ?? 0) > 0 && `；评委有 ${item.cost.judge_usage_missing_calls} 次调用用量未知`}</>
                        ) : (
                          <>实测 {formatReceiptTokens(item.budget.actual_tokens)}；未知调用 {String(item.budget.usage_missing_calls ?? 0)}；未知用量估算 {formatReceiptTokens(item.budget.unknown_usage_estimated_tokens)}；触发验证独立于对照评测预算
                            {item.budget.budget_ledger_error ? `；成本台账写入错误：${item.budget.budget_ledger_error}` : ""}</>
                        )}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : (
                <p style={{ color: "#777", fontSize: 12 }}>对照评测和触发验证尚未完成，预算与成本明细将在收据更新后显示。</p>
              )}
              {receipt.requirement_coverage.length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary>需求覆盖明细</summary>
                  <ul style={{ paddingLeft: 20 }}>
                    {receipt.requirement_coverage.map((c: CheckResultView) => (
                      <li key={c.check_id}>
                        <code>{c.check_id}</code>：{CHECK_STATUS_LABEL[c.status] ?? c.status}
                        {c.evidence ? ` — ${c.evidence}` : ""}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              <div style={{ marginTop: 12 }}>
                {exportState !== "done" ? (
                  <button onClick={() => void exportZip()} disabled={exportState === "busy"}>
                    {exportState === "busy" ? "导出中…" : "导出交付包"}
                  </button>
                ) : (
                  <a
                    href={`/api/projects/${projectId}/skill-versions/${receipt.version_id}/export/download`}
                    style={{ color: "#06c" }}
                  >
                    下载交付包（ZIP）
                  </a>
                )}
              </div>
            </div>
          )}

          <button
            style={{ marginTop: 16 }}
            onClick={() => {
              if (timerRef.current) clearTimeout(timerRef.current);
              localStorage.removeItem(`${FLOW_KEY}:${projectId}`);
              setFlow(null);
              setReceipt(null);
              setExportState("idle");
              setRequestText("");
            }}
          >
            生成新技能
          </button>
        </div>
      )}
    </section>
  );
}

function formatReceiptTokens(value: unknown): string {
  return typeof value === "number" ? `${value.toLocaleString()} tokens` : "未知/未设置";
}

function formatReceiptLimit(value: unknown): string {
  if (!value || typeof value !== "object") return "执行器默认";
  const entries = Object.entries(value as Record<string, unknown>);
  const unique = [...new Set(entries.map(([, limit]) => limit))];
  if (unique.length === 1) return formatReceiptTokens(unique[0]);
  return entries.map(([id, limit]) => `${id}: ${formatReceiptTokens(limit)}`).join("，");
}
