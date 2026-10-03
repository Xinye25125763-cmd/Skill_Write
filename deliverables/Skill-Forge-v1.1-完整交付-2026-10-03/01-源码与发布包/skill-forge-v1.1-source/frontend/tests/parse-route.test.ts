// F03 回归：parseRoute 纯函数测试（node --experimental-strip-types 直接运行）
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

let failed = 0;
for (const [input, expected] of cases) {
  const actual = parseRoute(input);
  const ok = actual === expected;
  if (!ok) failed++;
  console.log(
    `${ok ? "PASS" : "FAIL"} parseRoute(${JSON.stringify(input)}) = ${JSON.stringify(actual)}（期望 ${JSON.stringify(expected)}）`,
  );
}
if (failed > 0) {
  console.error(`\n${failed} 个用例失败`);
  process.exit(1);
}
console.log(`\n全部 ${cases.length} 个用例通过`);
