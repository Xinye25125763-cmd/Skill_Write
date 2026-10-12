import { useCallback, useEffect, useState } from "react";
import { api } from "./api.ts";
import type { BriefImportResult, BriefRevisionInfo, ProjectInfo } from "./types.ts";

/** 首页：项目列表 + 创建。 */
export function ProjectList({ onOpen }: { onOpen: (id: string) => void }) {
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setProjects(await api<ProjectInfo[]>("/api/projects"));
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const create = async () => {
    if (!name.trim()) return;
    try {
      await api<ProjectInfo>("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setName("");
      await refresh();
    } catch (err) {
      setError(String(err));
    }
  };

  return (
    <section>
      <h2>项目</h2>
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <input
          value={name}
          placeholder="新项目名称"
          onChange={(e) => setName(e.target.value)}
          style={{ padding: "6px 10px" }}
        />
        <button onClick={() => void create()}>创建</button>
      </div>
      {error && <p style={{ color: "#c33" }}>{error}</p>}
      {projects === null && <p>加载中…</p>}
      {projects?.length === 0 && <p>还没有项目，先创建一个。</p>}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {projects?.map((p) => (
          <li key={p.id} style={{ padding: "8px 0", borderBottom: "1px solid #eee" }}>
            <a
              href={`#/${p.id}`}
              onClick={() => onOpen(p.id)}
              style={{ fontSize: 16, cursor: "pointer" }}
            >
              {p.name}
            </a>
            <span style={{ color: "#888", marginLeft: 12 }}>
              {p.target_platform} · 需求修订 {p.latest_brief_revision_id ? "已导入" : "未导入"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** 需求页：导入 brief（人工粘贴降级路径）→ 三层分列 → 确认。 */
export function BriefPane({ projectId }: { projectId: string }) {
  const [raw, setRaw] = useState("");
  const [result, setResult] = useState<BriefImportResult | null>(null);
  const [revisions, setRevisions] = useState<BriefRevisionInfo[] | null>(null);
  const [error, setError] = useState("");

  const refreshRevisions = useCallback(async () => {
    try {
      setRevisions(await api<BriefRevisionInfo[]>(`/api/projects/${projectId}/briefs`));
    } catch (err) {
      setError(String(err));
    }
  }, [projectId]);

  useEffect(() => {
    void refreshRevisions();
  }, [refreshRevisions]);

  const importBrief = async () => {
    setError("");
    try {
      const brief = JSON.parse(raw);
      setResult(
        await api<BriefImportResult>(`/api/projects/${projectId}/briefs`, {
          method: "POST",
          body: JSON.stringify({ brief }),
        }),
      );
      setRaw("");
      await refreshRevisions();
    } catch (err) {
      setError(String(err));
    }
  };

  const confirm = async (revisionId: string) => {
    setError("");
    try {
      setResult(
        await api<BriefImportResult>(`/api/projects/${projectId}/briefs/${revisionId}`, {
          method: "PATCH",
          body: JSON.stringify({ edits: {} }),
        }),
      );
      await refreshRevisions();
    } catch (err) {
      setError(String(err));
    }
  };

  const column = (title: string, paths: string[] | undefined, color: string) => (
    <div style={{ flex: 1, border: "1px solid #ddd", padding: 12, borderRadius: 6 }}>
      <h4 style={{ marginTop: 0, color }}>{title}</h4>
      {paths?.length === 0 && <p style={{ color: "#aaa" }}>（空）</p>}
      <ul style={{ paddingLeft: 18, margin: 0 }}>
        {paths?.map((p) => (
          <li key={p}>
            <code>{p}</code>
          </li>
        ))}
      </ul>
    </div>
  );

  return (
    <section>
      <h2>需求</h2>
      <p style={{ color: "#888" }}>
        S2 阶段为人工导入路径：在别处生成结构化 brief JSON 后粘贴到此处。
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        rows={8}
        placeholder='粘贴 brief JSON，例如 {"purpose": {"value": "...", "source": {...}}, ...}'
        style={{ width: "100%", fontFamily: "monospace", fontSize: 13 }}
      />
      <div style={{ margin: "8px 0" }}>
        <button onClick={() => void importBrief()} disabled={!raw.trim()}>
          导入草案
        </button>
      </div>
      {error && <p style={{ color: "#c33" }}>{error}</p>}

      {result && (
        <div>
          <div style={{ display: "flex", gap: 12, marginTop: 16 }}>
            {column("已明确", result.explicit, "#2a7")}
            {column("系统推断", result.inferred, "#c80")}
            {column("待确认", result.to_confirm, "#c33")}
          </div>
          {result.issues.length > 0 && (
            <div style={{ marginTop: 12, color: "#c33" }}>
              <b>校验问题：</b>
              <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
                {result.issues.map((i) => (
                  <li key={i}>{i}</li>
                ))}
              </ul>
            </div>
          )}
          {result.status === "draft" && (
            <button style={{ marginTop: 12 }} onClick={() => void confirm(result.revision_id)}>
              确认这份修订
            </button>
          )}
        </div>
      )}

      <h3 style={{ marginTop: 24 }}>修订历史</h3>
      <ul style={{ paddingLeft: 18 }}>
        {revisions?.map((r) => (
          <li key={r.id}>
            <code>{r.id.slice(-6)}</code> · {r.status} · {r.created_at.slice(0, 19)}
          </li>
        ))}
        {revisions?.length === 0 && <li>（无）</li>}
      </ul>
    </section>
  );
}
