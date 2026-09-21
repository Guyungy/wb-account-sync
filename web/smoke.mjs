/**
 * 对着**真实后端**跑一遍数据层的冒烟。
 *
 * 为什么值得单独有这个东西：界面「看起来对」不算数 —— 页面能打开、
 * 样式没崩，但字段名写错了照样一片空白。这个脚本直接调
 * `dist/api.js`，断言的是真实响应的**字段形状**与**错误路径**。
 *
 * 只跑只读端点（state / survey）。计划、执行、回滚一概不碰 ——
 * 冒烟不该有副作用。
 *
 * 用法：
 *   node smoke.mjs <base-url> <token>
 * 或者把启动时打印的完整链接直接给进来：
 *   node smoke.mjs 'http://127.0.0.1:51234/?t=xxxx'
 */

import { BridgeClient, ApiError } from "./dist/api.js";

const arg = process.argv[2];
if (!arg) {
  console.error("用法：node smoke.mjs 'http://127.0.0.1:<port>/?t=<token>'");
  process.exit(2);
}

const parsed = new URL(arg);
const token = parsed.searchParams.get("t");
if (!token) {
  console.error("链接里没有 t= 参数，无法鉴权");
  process.exit(2);
}

const client = new BridgeClient(parsed.origin, token);
let failures = 0;

function check(label, ok, detail = "") {
  if (ok) {
    console.log(`  ✓ ${label}`);
  } else {
    failures += 1;
    console.error(`  ✗ ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

console.log("对真实后端冒烟：");

// --- /api/state -----------------------------------------------------------
const state = await client.getState();
check("state.platform 是非空字符串", typeof state.platform === "string" && state.platform.length > 0);
check("state.clients 是数组", Array.isArray(state.clients));
check("state.clients 有两个客户端", state.clients.length === 2, `实得 ${state.clients.length}`);
check("state.all_stopped 是布尔", typeof state.all_stopped === "boolean");
check("state.running_names 是数组", Array.isArray(state.running_names));

const first = state.clients[0];
if (!first) {
  check("clients[0] 存在", false);
} else {
  // 这几个字段是渲染层直接用的，缺一个页面就会显示 undefined。
  for (const key of ["key", "display", "running", "home", "home_exists", "db_exists", "home_note"]) {
    check(`clients[0].${key} 存在`, key in first, `键集合：${Object.keys(first).join(",")}`);
  }
  check(
    "all_stopped 与 running 一致",
    state.all_stopped === state.clients.every((c) => !c.running),
    `all_stopped=${state.all_stopped}`,
  );
}

// --- /api/survey ----------------------------------------------------------
const survey = await client.survey();
check("survey 返回对象", survey !== null && typeof survey === "object");

// --- 错误路径 -------------------------------------------------------------
// 这条比成功路径更重要：错误文案是直接展示给用户的，
// 如果客户端把服务端的说明吞掉换成"请求失败"，用户就不知道该怎么办。
try {
  await new BridgeClient(parsed.origin, "definitely-wrong-token").getState();
  check("错误 token 应当被拒绝", false, "竟然放行了");
} catch (err) {
  check("错误 token 抛 ApiError", err instanceof ApiError);
  check("错误 token 返回 403", err instanceof ApiError && err.status === 403, String(err));
  check(
    "错误文案来自服务端",
    err instanceof ApiError && err.message.includes("token"),
    err instanceof Error ? err.message : String(err),
  );
}

console.log(failures === 0 ? "\n冒烟通过。" : `\n冒烟失败：${failures} 项`);
process.exit(failures === 0 ? 0 : 1);
