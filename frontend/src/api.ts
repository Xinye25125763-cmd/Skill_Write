/** API 客户端：自动附带会话令牌。 */

const TOKEN_KEY = "skillforge_token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

/** 从 URL 片段取启动器下发的令牌，存 localStorage 后清除地址栏痕迹。 */
export function initTokenFromUrl(): string {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const fromUrl = params.get("token");
  if (fromUrl) {
    localStorage.setItem(TOKEN_KEY, fromUrl);
    history.replaceState(null, "", window.location.pathname);
  }
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "x-session-token": getToken(),
      ...(init?.headers ?? {}),
    },
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const message =
      body?.detail?.errors?.join("; ") ?? body?.detail?.message ?? `HTTP ${resp.status}`;
    throw new Error(message);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}
