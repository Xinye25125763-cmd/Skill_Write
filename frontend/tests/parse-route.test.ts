// F03 回归：parseRoute 纯函数测试（vitest 运行；原 node 脚本式断言已入 vitest 套件）
import { test, expect } from "vitest";
import { parseRoute } from "../src/route.ts";

const cases: Array<[string, string | null]> = [
  ["", null], // 空 hash → 首页
  ["#", null], // 裸 # → 首页
  ["#/", null], // F03 核心场景：#/ 不再被当成空项目 ID
  ["#/abc123", "abc123"],
  ["#/01M3ZQDRTA9HBDHBFWNEPRYMXG", "01M3ZQDRTA9HBDHBFWNEPRYMXG"],
  ["#/a/b", "a/b"], // 多段保留原样（由后端 404 处理）
  ["#foo", null], // 非 #/ 前缀 → 首页
  ["#/ ", " "], // 含空白的非空 ID 保持非空语义
];

test("parseRoute 回归用例", () => {
  for (const [input, expected] of cases) {
    expect(parseRoute(input), `parseRoute(${JSON.stringify(input)})`).toBe(expected);
  }
});
