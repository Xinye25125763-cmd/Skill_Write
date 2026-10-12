/** S3 类型：草稿与版本。 */

export interface DraftFile {
  path: string;
  size: number;
}

export interface VersionFile {
  path: string;
  hash: string;
  size: number;
}

export interface SkillVersionInfo {
  id: string;
  label: string | null;
  parent_version_id: string | null;
  brief_revision_id: string | null;
  content_hash: string;
  warnings: string[];
  status: string;
  created_at: string;
  files?: VersionFile[];
}

export interface VersionCreateResult extends SkillVersionInfo {
  files: VersionFile[];
}

/** S4 类型：异步任务视图（/api/jobs/{id}）。 */
export interface JobView {
  id: string;
  type: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  created_at: string;
  updated_at: string;
  lease_expires_at: string | null;
  cancel_requested: boolean;
  result: VersionCreateResult | null;
  error: { code: string; message?: string; errors?: string[]; existing_version_id?: string } | null;
}
