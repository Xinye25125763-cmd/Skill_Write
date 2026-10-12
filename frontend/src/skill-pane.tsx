import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.ts";
import type { DraftFile, JobView, SkillVersionInfo, VersionCreateResult } from "./types-skills.ts";

/** 技能编辑器：左文件树 + 中编辑器 + 右校验/版本区（主§5.3）。 */
export function SkillPane({ projectId }: { projectId: string }) {
  const [files, setFiles] = useState<DraftFile[] | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [newPath, setNewPath] = useState("");
  const [versions, setVersions] = useState<SkillVersionInfo[] | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const jobPollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (jobPollTimer.current) clearTimeout(jobPollTimer.current);
  }, []);

  const refreshTree = useCallback(async () => {
    try {
      const data = await api<{ files: DraftFile[] }>(`/api/projects/${projectId}/skills/draft`);
      setFiles(data.files);
      return data.files;
    } catch (err) {
      setMessage({ kind: "err", text: String(err) });
      return null;
    }
  }, [projectId]);

  const refreshVersions = useCallback(async () => {
    try {
      setVersions(await api<SkillVersionInfo[]>(`/api/projects/${projectId}/skills/versions`));
    } catch (err) {
      setMessage({ kind: "err", text: String(err) });
    }
  }, [projectId]);

  useEffect(() => {
    void refreshTree();
    void refreshVersions();
  }, [refreshTree, refreshVersions]);

  const openFile = async (path: string) => {
    try {
      const data = await api<{ path: string; content: string }>(
        `/api/projects/${projectId}/skills/draft/files/${path}`,
      );
      setCurrent(path);
      setContent(data.content);
      setDirty(false);
    } catch (err) {
      setMessage({ kind: "err", text: String(err) });
    }
  };

  /** 自动保存：失焦/停顿即写回（主§5.3 自动保存只保存工作草稿）。 */
  const save = async (path: string, text: string) => {
    try {
      await api(`/api/projects/${projectId}/skills/draft/files/${path}`, {
        method: "PUT",
        body: JSON.stringify({ content: text }),
      });
      setDirty(false);
      await refreshTree();
    } catch (err) {
      setMessage({ kind: "err", text: `保存失败：${err}` });
    }
  };

  const createFile = async () => {
    const path = newPath.trim();
    if (!path) return;
    try {
      await api(`/api/projects/${projectId}/skills/draft/files/${path}`, {
        method: "PUT",
        body: JSON.stringify({ content: "" }),
      });
      setNewPath("");
      await refreshTree();
      await openFile(path);
    } catch (err) {
      setMessage({ kind: "err", text: String(err) });
    }
  };

  const deleteFile = async (path: string) => {
    if (!window.confirm(`删除草稿文件 ${path}？`)) return;
    try {
      await api(`/api/projects/${projectId}/skills/draft/files/${path}`, { method: "DELETE" });
      if (current === path) {
        setCurrent(null);
        setContent("");
      }
      await refreshTree();
    } catch (err) {
      setMessage({ kind: "err", text: String(err) });
    }
  };

  /** 轮询任务直到终态（S4：版本创建为异步任务，202 → job 视图）。 */
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
            // 最多约 3 分钟，防止无限轮询
            reject(new Error(`任务 ${jobId} 长时间未完成（${job.status}）`));
            return;
          }
          jobPollTimer.current = setTimeout(() => void tick(n + 1), 300);
        } catch (err) {
          reject(err);
        }
      };
      void tick(0);
    });

  const createVersion = async () => {
    if (current && dirty) await save(current, content);
    setCreating(true);
    setMessage({ kind: "ok", text: "已提交版本创建任务，等待 Worker 执行…" });
    try {
      const resp = await api<{ job_id: string; status: string }>(
        `/api/projects/${projectId}/skills/versions`,
        {
          method: "POST",
          body: JSON.stringify({ label: `v${(versions?.length ?? 0) + 1}` }),
        },
      );
      const job = await waitForJob(resp.job_id);
      if (job.status === "succeeded" && job.result) {
        const v: VersionCreateResult = job.result;
        setMessage({
          kind: "ok",
          text: `版本已创建：${v.label ?? v.id}（${v.files.length} 个文件，哈希 ${v.content_hash.slice(0, 12)}…）${v.warnings.length ? `，警告：${v.warnings.join("；")}` : ""}`,
        });
        await refreshVersions();
      } else {
        const err: JobView["error"] = job.error ?? null;
        const detail =
          err?.errors?.join("；") ??
          err?.message ??
          `任务终态：${job.status}`;
        setMessage({
          kind: "err",
          text: `创建失败（${err?.code ?? job.status}）：${detail}`,
        });
      }
    } catch (err) {
      setMessage({ kind: "err", text: `创建失败：${err}` });
    } finally {
      setCreating(false);
    }
  };

  return (
    <section>
      <h2>技能</h2>
      {message && (
        <p style={{ color: message.kind === "ok" ? "#2a7" : "#c33", whiteSpace: "pre-wrap" }}>
          {message.text}
        </p>
      )}
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        {/* 左：文件树 */}
        <div style={{ width: 240, flexShrink: 0 }}>
          <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
            <input
              value={newPath}
              placeholder="新文件路径，如 SKILL.md"
              onChange={(e) => setNewPath(e.target.value)}
              style={{ flex: 1, padding: "4px 8px", fontSize: 13 }}
            />
            <button onClick={() => void createFile()}>+</button>
          </div>
          {files?.length === 0 && <p style={{ color: "#aaa" }}>草稿为空，从 SKILL.md 开始。</p>}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 14 }}>
            {files?.map((f) => (
              <li
                key={f.path}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "4px 8px",
                  borderRadius: 4,
                  background: current === f.path ? "#eef" : "transparent",
                  cursor: "pointer",
                }}
                onClick={() => void openFile(f.path)}
              >
                <span>{f.path}</span>
                <button
                  style={{ marginLeft: 8, color: "#c33", border: "none", background: "none", cursor: "pointer" }}
                  onClick={(e) => {
                    e.stopPropagation();
                    void deleteFile(f.path);
                  }}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </div>

        {/* 中：编辑器 */}
        <div style={{ flex: 1 }}>
          {current === null ? (
            <p style={{ color: "#aaa" }}>选择左侧文件或新建。</p>
          ) : (
            <>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <code>{current}</code>
                <span>
                  {dirty && <span style={{ color: "#c80" }}>未保存 </span>}
                  <button onClick={() => void save(current, content)} disabled={!dirty}>
                    保存草稿
                  </button>
                </span>
              </div>
              <textarea
                value={content}
                onChange={(e) => {
                  setContent(e.target.value);
                  setDirty(true);
                }}
                onBlur={() => dirty && void save(current, content)}
                rows={20}
                style={{ width: "100%", fontFamily: "monospace", fontSize: 13 }}
              />
            </>
          )}
          <div style={{ marginTop: 12 }}>
            <button onClick={() => void createVersion()} disabled={creating || !files?.length}>
              {creating ? "创建中…" : "创建不可变版本"}
            </button>
          </div>
        </div>

        {/* 右：版本列表 */}
        <div style={{ width: 280, flexShrink: 0 }}>
          <h4 style={{ marginTop: 0 }}>版本</h4>
          {versions?.length === 0 && <p style={{ color: "#aaa" }}>尚无版本。</p>}
          <ul style={{ listStyle: "none", padding: 0, fontSize: 13 }}>
            {versions?.map((v) => (
              <li key={v.id} style={{ padding: "6px 0", borderBottom: "1px solid #eee" }}>
                <b>{v.label ?? v.id.slice(-6)}</b>
                <span style={{ color: "#888" }}> · {v.created_at.slice(0, 19)}</span>
                <div style={{ color: "#aaa", fontSize: 11 }}>
                  哈希 {v.content_hash.slice(0, 12)}…
                  {v.warnings.length > 0 && (
                    <div style={{ color: "#c80" }}>⚠ {v.warnings.length} 条警告</div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
