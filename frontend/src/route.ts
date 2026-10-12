/** 路由解析（hash 路由）。

F03：统一解析规则——`#/`、空 hash 及任何非 `#/…` 形式都归首页（null）；
只有 `#/<项目ID>`（非空）才进入项目页。修复旧实现把 `#/` 解析为空串、
被当作项目 ID 渲染项目面板的问题。
 */
export function parseRoute(hash: string): string | null {
  if (!hash.startsWith("#/")) return null;
  const id = hash.slice(2);
  return id.length > 0 ? id : null;
}
