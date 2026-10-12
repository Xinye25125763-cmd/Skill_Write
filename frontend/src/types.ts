/** S2 类型定义：与后端路由响应形状一致。 */

export interface ProjectInfo {
  id: string;
  name: string;
  target_platform: string;
  created_at: string;
  latest_brief_revision_id: string | null;
}

export interface BriefImportResult {
  revision_id: string;
  status: "draft" | "confirmed";
  explicit: string[];
  inferred: string[];
  to_confirm: string[];
  issues: string[];
}

export interface BriefRevisionInfo {
  id: string;
  parent_revision_id: string | null;
  status: "draft" | "confirmed";
  created_at: string;
}
