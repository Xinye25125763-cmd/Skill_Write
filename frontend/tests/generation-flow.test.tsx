/**
 * T17：网页自然语言入口与完整状态呈现（前端验证代替 pytest）。
 *
 * 本项验收（工程计划 §5 T17）：
 * 1. 输入需求后显示阶段和证据（功能清单/规则/费用/失败原因）；
 * 2. 只问必要问题（仅渲染 pending_questions，不追问多余项）；
 * 3. 刷新可恢复（flow_id 本地持久化，重挂载后恢复轮询显示）；
 * 4. cancel 反馈真实（调用取消接口并显示真实终态）；
 * 5. 区分候选/静态通过/运行通过/宿主通过（五维状态分列呈现）；
 * 6. 等待结束（终态）不自动取消后台，也不再轮询；
 * 7. 导出交付包并给出下载链接。
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api.ts";

vi.mock("../src/api.ts", () => ({ api: vi.fn() }));
const apiMock = vi.mocked(api);

import { GenerationPane } from "../src/generation-pane.tsx";

const PID = "01TESTPROJECT00000000000000A";

function flowView(overrides: Record<string, unknown> = {}) {
  return {
    flow_id: "flow-1",
    project_id: PID,
    state: "extracting",
    revision: 2,
    budget: { used_tokens: 1200, used_model_calls: 2 },
    checkpoint: {},
    pending_questions: [],
    error: null,
    job_id: "job-1",
    ...overrides,
  };
}

const AWAITING = flowView({
  state: "awaiting_input",
  revision: 3,
  pending_questions: ["必要问题 1：输出语言用中文还是英文？"],
  contract: {
    requirements: [
      { requirement_id: "REQ-1", content: "整理会议纪要为行动项", necessary: true },
    ],
  },
  profile: { profile_id: "default", revision: 2, rules: [{ rule_id: "SKILL_HEADER" }] },
});

const RECEIPT = {
  flow_id: "flow-1",
  version_id: "01TESTVERSION0000000000",
  delivery_status: "candidate",
  static_status: "passed",
  execution_status: "not_run",
  coverage_status: "passed",
  host_status: null,
  zip_path: "",
  requirement_coverage: [],
  notes: ["execution=not_run：运行验证未执行（not run），完整验收尚未完成"],
};

const DELIVERED = flowView({
  state: "delivered",
  revision: 8,
  budget: { used_tokens: 5200, used_model_calls: 4 },
  checkpoint: { receipt: RECEIPT },
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("GenerationPane（T17 网页自然语言入口）", () => {
  beforeEach(() => {
    apiMock.mockReset();
    localStorage.clear();
  });
  afterEach(() => cleanup());

  it("test_ui_resume_required_question_and_candidate_delivery", async () => {
    let phase = "creating";
    const createdBodies: any[] = [];
    const answerBodies: any[] = [];
    const cancelBodies: any[] = [];
    let getCalls = 0;

    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      if (path === `/api/projects/${PID}/generation-flows` && init?.method === "POST") {
        createdBodies.push(body);
        phase = "extracting";
        return flowView();
      }
      if (path === "/api/generation-flows/flow-1" && init === undefined) {
        getCalls += 1;
        if (phase === "extracting") return AWAITING;
        if (phase === "answered") return DELIVERED;
        return DELIVERED;
      }
      if (path === "/api/generation-flows/flow-1/answers" && init?.method === "POST") {
        answerBodies.push(body);
        phase = "answered";
        return AWAITING;
      }
      if (path === `/api/projects/${PID}/skill-versions/01TESTVERSION0000000000/export`
          && init?.method === "POST") {
        return { ...RECEIPT, zip_path: "E:/exports/v1.zip" };
      }
      throw new Error(`意外的 API 调用: ${path} ${init?.method ?? "GET"}`);
    });

    // ---------- 块 A：输入需求 → 显示阶段与证据 ----------
    render(<GenerationPane projectId={PID} pollMs={30} />);
    fireEvent.change(screen.getByPlaceholderText(/自然语言描述/), {
      target: { value: "把会议纪要整理成行动项" },
    });
    fireEvent.click(screen.getByRole("button", { name: /开始生成/ }));

    await waitFor(() => {
      expect(createdBodies[0]?.request_text).toBe("把会议纪要整理成行动项");
    });
    // 阶段 + 功能清单 + 规则 + 费用（证据要素）可见
    await waitFor(() => {
      expect(screen.getByText(/awaiting_input|等待补充/)).toBeTruthy();
    });
    expect(screen.getByText("整理会议纪要为行动项")).toBeTruthy(); // 功能清单
    expect(screen.getByText(/SKILL_HEADER/)).toBeTruthy(); // 规则版本
    expect(screen.getByText(/1200|5200/)).toBeTruthy(); // 费用（used_tokens）

    // ---------- 块 B：只问必要问题 ----------
    expect(screen.getByText(/输出语言用中文还是英文/)).toBeTruthy();
    const questionItems = screen.getAllByRole("listitem").filter((el) =>
      /输出语言用中文还是英文/.test(el.textContent ?? ""));
    expect(questionItems.length).toBe(1); // 仅必要问题，不多问

    // ---------- 块 B2：回答必要问题（revision CAS + 幂等键） ----------
    fireEvent.change(screen.getByPlaceholderText(/你的回答/), {
      target: { value: "用中文" },
    });
    fireEvent.click(screen.getByRole("button", { name: /提交回答/ }));
    await waitFor(() => expect(answerBodies.length).toBe(1));
    expect(answerBodies[0].revision).toBe(3);
    expect(answerBodies[0].idempotency_key).toBeTruthy();
    expect(answerBodies[0].answers).toBeTruthy();

    // ---------- 块 C：交付收据五维区分（候选/静态/运行/宿主） ----------
    await waitFor(() => {
      expect(screen.getByText(/delivered|已交付候选|交付完成/)).toBeTruthy();
    });
    expect(screen.getAllByText(/candidate/).length).toBeGreaterThan(0); // 未验证候选如实标注
    expect(screen.getAllByText(/静态/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/运行/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/宿主/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/not run|未运行/).length).toBeGreaterThan(0); // 运行维度未运行如实显示
    expect(screen.getByText(/5200/)).toBeTruthy(); // 累计费用

    // ---------- 块 D：导出 + 下载 ----------
    fireEvent.click(screen.getByRole("button", { name: /导出交付包/ }));
    await waitFor(() => {
      const link = screen.getByRole("link", { name: /下载/ }) as HTMLAnchorElement;
      expect(link.href).toContain("/export/download");
    });

    // ---------- 块 E：终态不再轮询、不自动取消 ----------
    const callsAtRest = getCalls;
    await sleep(200);
    expect(getCalls).toBe(callsAtRest); // 终态停止轮询
    const cancelCalled = cancelBodies.length === 0
      && !apiMock.mock.calls.some(([p, i]) => p.endsWith("/cancel"));
    expect(cancelCalled).toBe(true); // 等待结束绝不自动取消后台

    // ---------- 块 F：刷新可恢复（重新挂载即恢复显示） ----------
    cleanup();
    render(<GenerationPane projectId={PID} pollMs={30} />);
    await waitFor(() => {
      expect(screen.getAllByText(/candidate/).length).toBeGreaterThan(0); // 恢复出交付状态
    });

    // ---------- 块 G：cancel 反馈真实 ----------
    localStorage.clear();
    phase = "generating";
    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      if (path === `/api/projects/${PID}/generation-flows` && init?.method === "POST") {
        return flowView({ state: "generating", revision: 5 });
      }
      if (path === "/api/generation-flows/flow-1" && init === undefined) {
        return phase === "generating"
          ? flowView({ state: "generating", revision: 5 })
          : flowView({ state: "cancelled", revision: 6 });
      }
      if (path === "/api/generation-flows/flow-1/cancel" && init?.method === "POST") {
        cancelBodies.push(body);
        phase = "cancelled";
        return flowView({ state: "cancelled", revision: 6 });
      }
      throw new Error(`意外的 API 调用: ${path}`);
    });
    cleanup();
    render(<GenerationPane projectId={PID} pollMs={30} />);
    fireEvent.change(screen.getByPlaceholderText(/自然语言描述/), {
      target: { value: "第二个需求" },
    });
    fireEvent.click(screen.getByRole("button", { name: /开始生成/ }));
    await waitFor(() => {
      expect(screen.getByText(/generating|生成中/)).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /取消/ }));
    await waitFor(() => {
      expect(screen.getByText(/cancelled|已取消/)).toBeTruthy();
    });
    expect(cancelBodies.length).toBe(1);
    expect(cancelBodies[0].revision).toBe(5); // CAS 取消
  });

  // ---------- 块 H：提交 500 → 如实显示错误且可重试 ----------
  it("test_submit_server_error_shown_and_retryable", async () => {
    const createdBodies: any[] = [];
    let failFirst = true;

    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      if (path === `/api/projects/${PID}/generation-flows` && init?.method === "POST") {
        if (failFirst) {
          failFirst = false;
          throw new Error("HTTP 500"); // 服务端错误如实上抛（api.ts 语义）
        }
        createdBodies.push(body);
        return flowView();
      }
      if (path === "/api/generation-flows/flow-1" && init === undefined) {
        return DELIVERED; // 终态即停，避免后台轮询干扰断言
      }
      throw new Error(`意外的 API 调用: ${path} ${init?.method ?? "GET"}`);
    });

    render(<GenerationPane projectId={PID} pollMs={30} />);
    fireEvent.change(screen.getByPlaceholderText(/自然语言描述/), {
      target: { value: "把会议纪要整理成行动项" },
    });
    fireEvent.click(screen.getByRole("button", { name: /开始生成/ }));

    // 错误如实显示（非吞掉、非伪成功），入口仍在（flow 未被置位）
    await waitFor(() => {
      expect(screen.getByText(/HTTP 500/)).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: /开始生成/ })).toBeTruthy();
    expect(localStorage.getItem(`skillforge_generation_flow:${PID}`)).toBeNull();

    // 重试成功：错误清除，进入流程态
    fireEvent.click(screen.getByRole("button", { name: /开始生成/ }));
    await waitFor(() => expect(createdBodies.length).toBe(1));
    await waitFor(() => {
      expect(screen.getByText(/delivered|已交付候选|交付完成/)).toBeTruthy();
    });
    expect(screen.queryByText(/HTTP 500/)).toBeNull();
  });

  // ---------- 块 I：恢复轮询 404 → 清 stale flow_id，回到入口态 ----------
  it("test_stale_flow_id_cleared_on_restore_failure", async () => {
    localStorage.setItem(`skillforge_generation_flow:${PID}`, "flow-deleted");
    let getCalls = 0;

    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/generation-flows/flow-deleted" && init === undefined) {
        getCalls += 1;
        throw new Error("HTTP 404"); // 流程已删除
      }
      throw new Error(`意外的 API 调用: ${path} ${init?.method ?? "GET"}`);
    });

    render(<GenerationPane projectId={PID} pollMs={30} />);

    // stale 被清除，回到入口态（可重新发起），且不重复重放 404
    await waitFor(() => {
      expect(getCalls).toBe(1);
    });
    expect(localStorage.getItem(`skillforge_generation_flow:${PID}`)).toBeNull();
    expect(screen.getByPlaceholderText(/自然语言描述/)).toBeTruthy();
    await sleep(150);
    expect(getCalls).toBe(1); // 不再轮询已删除的流程
  });

  // ---------- 块 J：网络离线 → 轮询指数退避（串行，不风暴），恢复即正常 ----------
  it("test_offline_polling_backs_off_without_storm_and_recovers", async () => {
    let offline = true;
    let getCalls = 0;

    apiMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === `/api/projects/${PID}/generation-flows` && init?.method === "POST") {
        return flowView({ state: "generating", revision: 5 });
      }
      if (path === "/api/generation-flows/flow-1" && init === undefined) {
        getCalls += 1;
        if (offline) throw new Error("Failed to fetch"); // 离线
        return DELIVERED;
      }
      throw new Error(`意外的 API 调用: ${path} ${init?.method ?? "GET"}`);
    });

    render(<GenerationPane projectId={PID} pollMs={30} />);
    fireEvent.change(screen.getByPlaceholderText(/自然语言描述/), {
      target: { value: "把会议纪要整理成行动项" },
    });
    fireEvent.click(screen.getByRole("button", { name: /开始生成/ }));
    await waitFor(() => {
      expect(screen.getByText(/generating|生成中/)).toBeTruthy();
    });

    // 离线窗口内：指数退避（30→60→120→240→480…ms），
    // 500ms 内至多 4~5 次尝试；若无退避将 ~15 次以上
    await sleep(500);
    const callsDuringOffline = getCalls;
    expect(callsDuringOffline).toBeGreaterThan(0);
    expect(callsDuringOffline).toBeLessThanOrEqual(6);
    expect(screen.getByText(/Failed to fetch/)).toBeTruthy(); // 错误如实显示

    // flow 不被离线错误清掉（stale 清理只属于恢复路径的 404）
    expect(localStorage.getItem(`skillforge_generation_flow:${PID}`)).toBe("flow-1");

    // 网络恢复：下一次轮询成功 → 退避重置 → 正常显示终态并停止
    offline = false;
    await waitFor(() => {
      expect(screen.getByText(/delivered|已交付候选|交付完成/)).toBeTruthy();
    });
    const callsAtRest = getCalls;
    await sleep(200);
    expect(getCalls).toBe(callsAtRest); // 终态停止轮询
  });
});
