import { useCallback, useEffect, useState } from "react";
import { initTokenFromUrl } from "./api.ts";
import { parseRoute } from "./route.ts";
import { BriefPane, ProjectList } from "./panes.tsx";
import { SkillPane } from "./skill-pane.tsx";
import { RunsPane } from "./runs-pane.tsx";
import { EvalPane } from "./eval-pane.tsx";
import { GenerationPane } from "./generation-pane.tsx";

type HealthState =
  | { kind: "pending" }
  | { kind: "ok"; version: string; authed: boolean }
  | { kind: "error"; message: string };

export function App() {
  const [token] = useState(() => initTokenFromUrl());
  const [health, setHealth] = useState<HealthState>({ kind: "pending" });
  const [route, setRoute] = useState<string | null>(() => parseRoute(window.location.hash));

  const probe = useCallback(async () => {
    try {
      const healthResp = await fetch("/api/health");
      const healthJson = (await healthResp.json()) as { version: string };
      let authed = false;
      if (token) {
        const meResp = await fetch("/api/me", {
          headers: { "x-session-token": token },
        });
        authed = meResp.ok;
      }
      setHealth({ kind: "ok", version: healthJson.version, authed });
    } catch (err) {
      setHealth({ kind: "error", message: String(err) });
    }
  }, [token]);

  useEffect(() => {
    void probe();
  }, [probe]);

  useEffect(() => {
    const onHash = () => {
      setRoute(parseRoute(window.location.hash));
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (health.kind === "ok" && !health.authed) {
    return (
      <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem" }}>
        <h1>skill-forge</h1>
        <p>会话令牌缺失。请通过启动器（launcher.py）打开本页面。</p>
      </main>
    );
  }

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", maxWidth: 960 }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h1 style={{ cursor: "pointer" }} onClick={() => (window.location.hash = "#/")}>
          skill-forge
        </h1>
        <span style={{ color: "#888" }}>
          {health.kind === "ok" && `后端 v${health.version}`}
        </span>
      </header>

      {route === null && <ProjectList onOpen={(id) => (window.location.hash = `#/${id}`)} />}
      {route !== null && (
        <>
          <p>
            <a href="#/" style={{ color: "#06c", cursor: "pointer" }}>
              ← 返回项目列表
            </a>
          </p>
          <GenerationPane projectId={route} />
          <BriefPane projectId={route} />
          <SkillPane projectId={route} />
          <RunsPane projectId={route} />
          <EvalPane projectId={route} />
        </>
      )}

      {health.kind === "pending" && <p>正在连接后端…</p>}
      {health.kind === "error" && <p style={{ color: "#c33" }}>后端连接失败：{health.message}</p>}
      <footer style={{ marginTop: 48, color: "#aaa", fontSize: 12 }}>
        skill-forge · S6 · 个人版技能生产与评测工作台
      </footer>
    </main>
  );
}
