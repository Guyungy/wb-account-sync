/**
 * 界面入口 —— 第一块真实视图：客户端状态面板。
 *
 * 这一版**只做只读展示**：把两个客户端的探测结果、以及"能不能开始迁移"
 * 这个前置条件说清楚。执行迁移那条链路（计划 → 确认 → 执行 → 核验）
 * 随后接上。
 *
 * 为什么先从只读面板开始：它是整条流程的门槛 —— 写入前必须两个客户端
 * 都已完全退出，而这个状态随时在变（用户可能刚打开客户端）。
 * 把它的呈现做对，后面每一步的交互才有依托。
 */

import { ApiError, BridgeClient } from "./api.js";
import type { ClientStatus, StateResponse } from "./api.js";

const REFRESH_MS = 2000;

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`页面缺少 #${id} 节点`);
  return node as T;
}

/** 客户端的一行：名称 + 运行状态 + 数据目录。 */
function renderClient(client: ClientStatus): string {
  const state = client.running
    ? `<span class="pill running">运行中</span>`
    : `<span class="pill stopped">已退出</span>`;
  const homeState = client.home_exists
    ? client.db_exists
      ? ""
      : `<span class="warn">${escapeHtml(client.home_note)}</span>`
    : `<span class="warn">目录不存在</span>`;

  return `
    <div class="client">
      <div class="client-head">
        <strong>${escapeHtml(client.display)}</strong>
        ${state}
      </div>
      <div class="client-path" title="${escapeHtml(client.home)}">${escapeHtml(client.home)}</div>
      ${homeState}
    </div>`;
}

/** 顶部结论条：能不能开始。 */
function renderGate(state: StateResponse): string {
  if (state.all_stopped) {
    return `<div class="gate ok">两个客户端都已退出，可以开始迁移。</div>`;
  }
  const names = state.running_names.map(escapeHtml).join("、");
  return `<div class="gate blocked">${names} 仍在运行。请先完全退出客户端。</div>`;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function render(state: StateResponse): void {
  el("gate").innerHTML = renderGate(state);
  el("clients").innerHTML = state.clients.map(renderClient).join("");
  el("meta").textContent = `平台 ${state.platform} · 状态目录 ${state.state_dir}`;
}

function renderError(err: unknown): void {
  const message =
    err instanceof ApiError
      ? err.message
      : err instanceof Error
        ? err.message
        : String(err);
  el("gate").innerHTML = `<div class="gate blocked">${escapeHtml(message)}</div>`;
}

async function main(): Promise<void> {
  let client: BridgeClient;
  try {
    client = BridgeClient.fromLocation(window.location);
  } catch (err) {
    // token 拿不到就没必要轮询了 —— 每一次请求都只会是 403。
    renderError(err);
    return;
  }

  const tick = async (): Promise<void> => {
    try {
      render(await client.getState());
    } catch (err) {
      renderError(err);
    }
  };

  await tick();
  // 客户端可能随时被打开或关掉，所以这个状态必须持续刷新，
  // 而不是进入页面时取一次就定住。
  window.setInterval(() => void tick(), REFRESH_MS);
}

void main();
