export {};
declare global {
  interface Window {
    __TAURI__?: {
      core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
      event: { listen<T>(event: string, callback: (event: { payload: T }) => void): Promise<() => void> };
    };
  }
}

interface Client { variant: string; display: string; running: boolean; home: string; db_exists: boolean; current_uid: string | null }
interface Account { id: string; uid: string; variant: string; nickname: string; enabled: boolean; gateway_ready: boolean }
interface Status { clients: Client[]; all_stopped: boolean; pending_runs: { run_id: string; status: string }[] }
interface Pool { completed: number; errors: number; in_flight: Record<string, number> }
interface Plan { plan_id: string; source_sessions: number; new_copies: number; updated_copies: number; files: number; conflicts: { reason: string }[]; skipped: { reason: string }[] }
interface Login { login_id: string; verification_uri: string; expires_in: number }

function el<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`页面缺少 ${id}`);
  return node as T;
}
const escape = (s: string): string => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] ?? c));
const label = (v: string): string => v === "ai" ? "WorkBuddy AI" : "WorkBuddy";
let accounts: Account[] = [];
let status: Status | null = null;
let plan: Plan | null = null;
let busy = false;
let switching: Account | null = null;
let refreshing: Promise<void> | null = null;
let apiKey = "";
let accountFingerprint = "";
let statusFingerprint = "";
let loginTimer: ReturnType<typeof setTimeout> | null = null;

async function api<T>(path: string, body?: unknown): Promise<T> {
  if (!window.__TAURI__) throw new Error("请从本地桌面应用打开此界面。");
  try { return await window.__TAURI__.core.invoke<T>("request", { path, body: body ?? null }); }
  catch (error) { throw new Error(typeof error === "string" ? error : String(error)); }
}
function message(text: string, error = false): void {
  el("message").textContent = text;
  el("message").className = error ? "notice error" : "notice";
}
function showResult(value: unknown): void {
  el("result").textContent = JSON.stringify(value, null, 2);
  const result = value as { run_id?: string; sync?: { run_id?: string } };
  const run = result.run_id ?? result.sync?.run_id;
  if (run) {
    el<HTMLInputElement>("restore-id").value = run;
    try {
      const history: string[] = JSON.parse(localStorage.getItem("sync-runs") || "[]");
      localStorage.setItem("sync-runs", JSON.stringify([run, ...history.filter(id => id !== run)].slice(0, 8)));
    } catch { /* Storage may be unavailable. */ }
    renderHistory();
  }

}
async function action(work: () => Promise<unknown>, success: string, progress = "正在处理，请稍候…"): Promise<void> {
  if (busy) return;
  busy = true;
  document.querySelectorAll<HTMLInputElement>(".target, #sync-on-switch, #confirm-plan").forEach(input => { input.disabled = true; });
  document.querySelectorAll<HTMLButtonElement>("button:not(.tab)").forEach((b) => { b.disabled = true; });
  document.body.classList.add("working");
  el("message").setAttribute("aria-busy", "true");
  message(progress);
  try { const result = await work(); if (result !== undefined) showResult(result); message(success); }
  catch (err) { message(err instanceof Error ? err.message : String(err), true); }
  finally {
    busy = false;
    document.querySelectorAll<HTMLInputElement>(".target, #sync-on-switch, #confirm-plan").forEach(input => { input.disabled = false; });
    document.body.classList.remove("working");
    el("message").setAttribute("aria-busy", "false");
    document.querySelectorAll<HTMLButtonElement>("button:not(.tab)").forEach((b) => { b.disabled = b.dataset.unavailable === "true"; });
    await refresh().catch((err: unknown) => message(err instanceof Error ? err.message : String(err), true));
    updateApply();
  }
}
function renderStatus(value: Status): void {
  status = value;
  const fingerprint = JSON.stringify(value);
  if (fingerprint === statusFingerprint) { updateApply(); return; }
  statusFingerprint = fingerprint;
  el("connection").textContent = "本地桌面端";
  el("gate").innerHTML = `<div class="gate ${value.all_stopped ? "ok" : "blocked"}">${value.all_stopped ? "客户端已退出，可以执行会话同步。" : "客户端正在运行。账号切换会自动退出；独立会话同步需要先退出客户端。"}</div>`;
  el("clients").innerHTML = value.clients.map((c) => `<div class="client"><div class="client-head"><strong>${escape(c.display)}</strong><span class="pill ${c.running ? "running" : "stopped"}">${c.running ? "运行中" : "已退出"}</span></div><div class="client-path">${escape(c.home)}</div>${!c.db_exists ? '<span class="warn">尚未发现本地数据库</span>' : ""}</div>`).join("");
  el("pending-runs").replaceChildren();
  for (const run of value.pending_runs) {
    const button = document.createElement("button"); button.textContent = `恢复中断的同步 ${run.run_id}`; button.disabled = busy;
    button.addEventListener("click", () => { void action(() => api("/api/sessions/restore", { run_id: run.run_id }), "已恢复中断的同步。"); });
    el("pending-runs").append(button);
  }
  updateApply();
}
function renderAccounts(list: Account[]): void {
  const fingerprint = JSON.stringify([list, status?.clients.map(c => c.current_uid)]);
  if (fingerprint === accountFingerprint) return;
  accountFingerprint = fingerprint;
  const selected = new Set(Array.from(document.querySelectorAll<HTMLInputElement>(".target:checked")).map((c) => c.value));
  const previous = accounts.length; accounts = list;
  el("account-list").replaceChildren();
  if (list.length === 0) el("account-list").innerHTML = '<div class="empty">还没有保存账号。先保存当前登录态，或使用浏览器授权添加账号。</div>';
  for (const a of list) {
    const current = status?.clients.some((c) => c.variant === a.variant && c.current_uid === a.uid);
    const card = document.createElement("article"); card.className = "card account";
    card.innerHTML = `<div class="client-head"><h3>${escape(a.nickname)}</h3>${current ? '<span class="pill stopped">当前账号</span>' : ""}</div><p class="sub">${label(a.variant)} · ${escape(a.uid)}</p><p class="sub">${a.gateway_ready ? "可用于 API 网关" : "加密登录态：可切换；网关需要 OAuth 授权"}</p>`;
    const switchButton = document.createElement("button"); switchButton.className = "primary"; switchButton.textContent = current ? "正在使用" : "切换账号"; switchButton.disabled = busy || !!current;
    if (current) switchButton.dataset.unavailable = "true";
    switchButton.addEventListener("click", () => {
      switching = a;
      const sync = el<HTMLInputElement>("sync-on-switch").checked;
      el("switch-summary").textContent = `切换到 ${a.nickname}（${label(a.variant)}）。${sync ? "将退出两个客户端，并同步全部会话。" : "将退出目标客户端。"}登录态会自动备份，完成后重新打开客户端。`;
      el<HTMLDialogElement>("switch-dialog").showModal();
    });
    const toggle = document.createElement("button"); toggle.textContent = a.enabled ? "停用网关账号" : "启用网关账号"; toggle.disabled = busy;
    toggle.addEventListener("click", () => { void action(() => api("/api/accounts/enabled", { id: a.id, enabled: !a.enabled }), "网关账号状态已更新。"); });
    const buttons = document.createElement("div"); buttons.className = "toolbar"; buttons.append(switchButton, toggle); card.append(buttons); el("account-list").append(card);
  }
  el("sync-targets").replaceChildren();
  for (const a of list) {
    const item = document.createElement("label"); item.className = "check";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.className = "target"; checkbox.value = a.id; checkbox.checked = previous === 0 || selected.has(a.id);
    checkbox.addEventListener("change", () => { plan = null; el("plan-view").hidden = true; updateApply(); });
    item.append(checkbox, document.createTextNode(`${a.nickname} · ${label(a.variant)}`)); el("sync-targets").append(item);
  }
  filterAccounts();
  updateApply();
}
function renderPool(pool: Pool): void {
  const inflight = Object.values(pool.in_flight).reduce((a, b) => a + b, 0);
  el("gateway-stats").innerHTML = [["完成请求", pool.completed], ["上游失败", pool.errors], ["正在处理", inflight]].map(([name, value]) => `<div class="card stat"><span>${name}</span><strong>${value}</strong></div>`).join("");
}
async function fetchState(): Promise<void> {
  const [state, data] = await Promise.all([api<Status>("/api/state"), api<{ accounts: Account[]; pool: Pool }>("/api/accounts")]);
  renderStatus(state); renderAccounts(data.accounts); renderPool(data.pool);
}
async function refresh(): Promise<void> {
  if (refreshing) return refreshing;
  refreshing = fetchState().finally(() => { refreshing = null; });
  return refreshing;
}
function filterAccounts(): void {
  const query = el<HTMLInputElement>("account-search").value.trim().toLowerCase();
  const variant = el<HTMLSelectElement>("account-filter").value;
  let visible = 0;
  el("account-list").querySelectorAll<HTMLElement>(".account").forEach((card, index) => {
    const account = accounts[index];
    card.hidden = !account || (variant !== "all" && account.variant !== variant) || !`${account.nickname} ${account.uid}`.toLowerCase().includes(query);
    if (!card.hidden) visible++;
  });
  el("account-count").textContent = `${visible} / ${accounts.length}`;
}
function renderHistory(): void {
  const node = el("recent-runs"); node.replaceChildren();
  try {
    const history: string[] = JSON.parse(localStorage.getItem("sync-runs") || "[]");
    for (const id of history) {
      const button = document.createElement("button"); button.textContent = `选择备份 ${id.slice(0, 12)}…`;
      button.title = id;
      button.addEventListener("click", () => { el<HTMLInputElement>("restore-id").value = id; updateApply(); });
      node.append(button);
    }
  } catch { /* An invalid local history does not block startup. */ }
}
async function copyText(text: string, name: string): Promise<void> {
  try { await navigator.clipboard.writeText(text); message(`${name}已复制。`); }
  catch { message("复制失败，请选中内容后手动复制。", true); }
}
function updateApply(): void {
  el<HTMLButtonElement>("restore").disabled = busy || !el<HTMLInputElement>("restore-id").value.trim();
  el<HTMLButtonElement>("import-json").disabled = busy || !el<HTMLTextAreaElement>("account-json").value.trim();
  el<HTMLButtonElement>("make-plan").disabled = busy || !document.querySelector(".target:checked");
  const selected = document.querySelectorAll(".target:checked").length;
  el("selected-count").textContent = `已选择 ${selected} 个账号`;
  el("apply-hint").textContent = !status?.all_stopped ? "请先退出两个客户端，状态会自动更新。" : "客户端已退出，可以备份并执行。";
  el<HTMLButtonElement>("apply-plan").disabled = busy || !status?.all_stopped || !plan || !el<HTMLInputElement>("confirm-plan").checked;
  document.querySelectorAll<HTMLElement>(".steps span").forEach((step, index) => step.classList.toggle("current", index === (plan ? 1 : 0)));
}
function parseImport(text: string): unknown {
  try { return JSON.parse(text); } catch { throw new Error("账号内容不是有效 JSON。请粘贴完整的导出内容，检查括号和引号。"); }
}
function setup(): void {
  renderHistory();
  try { el<HTMLInputElement>("sync-on-switch").checked = localStorage.getItem("sync-on-switch") !== "false"; } catch { /* Default enabled. */ }
  el("sync-on-switch").addEventListener("change", () => { try { localStorage.setItem("sync-on-switch", String(el<HTMLInputElement>("sync-on-switch").checked)); } catch { /* Keep session preference. */ } });
  el("account-search").addEventListener("input", filterAccounts);
  el("account-filter").addEventListener("change", filterAccounts);
  for (const [id, checked] of [["select-all", true], ["select-none", false]] as const) {
    el(id).addEventListener("click", () => {
      document.querySelectorAll<HTMLInputElement>(".target").forEach(input => { input.checked = checked; });
      plan = null; el("plan-view").hidden = true; updateApply();
    });
  }
  el("cancel-switch").addEventListener("click", () => el<HTMLDialogElement>("switch-dialog").close());
  el("confirm-switch").addEventListener("click", () => {
    const account = switching; if (!account) return;
    el<HTMLDialogElement>("switch-dialog").close();
    void action(async () => {
      plan = null; el("plan-view").hidden = true;
      const result = await api<{ launch_error?: string }>("/api/accounts/switch", { id: account.id, sync_all: el<HTMLInputElement>("sync-on-switch").checked });
      showResult(result);
      if (result.launch_error) throw new Error(`登录态已切换，但自动打开失败：${result.launch_error}。请手动打开客户端。`);
      return result;
    }, `已切换到 ${account.nickname}，客户端已重新打开。`, "正在备份登录态、切换账号并重启客户端…");
  });
  el("copy-url").addEventListener("click", () => { void copyText(el<HTMLInputElement>("base-url").value, "接入地址"); });
  el("copy-example").addEventListener("click", () => { void copyText(el("gateway-example").textContent || "", "接入示例"); });
  el("add-account").addEventListener("click", () => el<HTMLDialogElement>("account-dialog").showModal());
  el("close-dialog").addEventListener("click", () => el<HTMLDialogElement>("account-dialog").close());
  document.querySelectorAll<HTMLButtonElement>(".tab").forEach((button) => button.addEventListener("click", () => {
    document.body.dataset.page = button.dataset.tab;
    el("page-label").textContent = button.textContent;
    document.querySelectorAll<HTMLElement>(".view").forEach((view) => { view.hidden = view.id !== button.dataset.tab; });
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === button));
  }));
  el("refresh").addEventListener("click", () => { void action(async () => { await refresh(); }, "账号列表已刷新。"); });
  el("import-local").addEventListener("click", () => { void action(() => api("/api/accounts/import-local", { variant: el<HTMLSelectElement>("variant").value }), "当前登录账号已保存。"); });
  el("import-json").addEventListener("click", () => { void action(async () => {
    const data: unknown = parseImport(el<HTMLTextAreaElement>("account-json").value);
    const result = await api("/api/accounts/import", { variant: el<HTMLSelectElement>("variant").value, data });
    el<HTMLDialogElement>("account-dialog").close();
    el<HTMLTextAreaElement>("account-json").value = ""; return result;
  }, "账号已导入。"); });
  el("oauth").addEventListener("click", () => { void action(async () => {
    if (loginTimer) clearTimeout(loginTimer);
    const login = await api<Login>("/api/accounts/oauth/start", { variant: el<HTMLSelectElement>("variant").value });
    const node = el("oauth-status"); node.hidden = false; node.replaceChildren();
    const link = document.createElement("button"); link.type = "button"; link.textContent = "打开官方授权页面";
    link.addEventListener("click", () => { void window.__TAURI__?.core.invoke("open_login", { url: login.verification_uri }).catch((error: unknown) => message(String(error), true)); });
    node.append(link, document.createTextNode("，完成授权后账号将自动出现在列表中。"));
    const expires = Date.now() + login.expires_in * 1000;
    const poll = async (): Promise<void> => {
      if (Date.now() > expires) { node.textContent = "授权已超时，请重新发起。"; return; }
      try {
        const result = await api<{ done: boolean }>("/api/accounts/oauth/poll", { login_id: login.login_id });
        if (result.done) { node.textContent = "授权完成，账号已保存。"; message("授权完成，账号已保存。"); el<HTMLDialogElement>("account-dialog").close(); await refresh(); return; }
        loginTimer = setTimeout(() => { void poll(); }, 2500);
      } catch (err) { node.textContent = err instanceof Error ? err.message : String(err); }
    };
    loginTimer = setTimeout(() => { void poll(); }, 2500);
  }, "请打开官方授权页面完成登录。"); });
  el("make-plan").addEventListener("click", () => { void action(async () => {
    const ids = Array.from(document.querySelectorAll<HTMLInputElement>(".target:checked")).map((c) => c.value);
    plan = await api<Plan>("/api/sessions/plan", { account_ids: ids });
    el("plan-view").hidden = false; el("plan-summary").textContent = `扫描 ${plan.source_sessions} 条会话 · 新建 ${plan.new_copies} 个副本 · 更新 ${plan.updated_copies} 个副本 · ${plan.files} 个文件`;
    el("plan-id").textContent = plan.plan_id; el<HTMLInputElement>("confirm-plan").checked = false;
    el("plan-view").scrollIntoView({ behavior: "smooth", block: "start" });
    el("plan-issues").replaceChildren();
    for (const item of [...plan.conflicts, ...plan.skipped]) { const p = document.createElement("p"); p.className = "warn"; p.textContent = item.reason; el("plan-issues").append(p); }
    return plan;
  }, "计划已生成，请检查冲突及跳过项。", "正在扫描会话、附件与文件历史，生成同步计划…"); });
  el("restore-id").addEventListener("input", updateApply);
  el("account-json").addEventListener("input", updateApply);
  el("confirm-plan").addEventListener("input", updateApply);
  el("apply-plan").addEventListener("click", () => { void action(async () => {
    const result = await api("/api/sessions/apply", { plan_id: plan?.plan_id }); plan = null; el("plan-view").hidden = true; return result;
  }, "同步已完成。备份已记录在恢复区域，可按需撤销。", "正在备份并同步会话，请保持应用打开…"); });
  el("restore").addEventListener("click", () => { void action(() => api("/api/sessions/restore", { run_id: el<HTMLInputElement>("restore-id").value.trim() }), "同步前的数据已恢复。"); });
  el("show-key").addEventListener("click", () => { const input = el<HTMLInputElement>("api-key"); input.type = input.type === "password" ? "text" : "password"; el("show-key").textContent = input.type === "password" ? "显示" : "隐藏"; });
  el("copy-key").addEventListener("click", () => { void copyText(apiKey, "API Key"); });
  el("models").addEventListener("click", () => { void action(async () => {
    const data = await api<{ data?: { id: string }[] }>("/api/gateway/models"); el("model-list").textContent = data.data?.map(model => model.id).join("\n") || "当前没有返回可用模型。";
  }, "可用模型列表已更新。"); });
}
async function main(): Promise<void> {
  try {
    document.body.dataset.page = "accounts";
    setup(); await refresh();
    const config = await api<{ api_key: string; base_url: string }>("/api/gateway"); apiKey = config.api_key; el<HTMLInputElement>("api-key").value = apiKey;
    const base = config.base_url; el<HTMLInputElement>("base-url").value = base;
    await window.__TAURI__?.event.listen<string>("operation-close-blocked", ({ payload }) => message(payload, true));
    el("gateway-example").textContent = `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="<API_KEY>")\nresponse = client.chat.completions.create(\n    model="<从可用模型列表选择>",\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(response.choices[0].message.content)`;
    window.setInterval(() => { if (!busy) void refresh().catch((err: unknown) => message(err instanceof Error ? err.message : String(err), true)); }, 5000);
  } catch (err) { el("connection").textContent = "连接失败"; message(err instanceof Error ? err.message : String(err), true); }
}
void main();
