/**
 * 后端接口的类型化客户端。
 *
 * ## 为什么要单独一个模块
 *
 * 界面的数据层与渲染层分开，有两个立刻能兑现的好处：
 *
 * 1. **契约可测**。这个模块在浏览器与 Node 里都能跑，所以可以直接对着
 *    真实后端跑冒烟（`web/smoke.mjs`），而不是靠"页面看起来对"来判断。
 * 2. **Tauri 之后仍是一份实现**。桌面壳加载的是同一个静态站点，
 *    数据层不需要为 Tauri 改写成 IPC —— 换壳不换实现。
 *
 * ## 鉴权
 *
 * 服务只绑 `127.0.0.1`，且要求 `?t=<token>`。token 每次启动重新生成，
 * 所以它只能来自当前页面 URL，不能硬编码。`t=` 缺失或错误一律 403。
 */

/** 服务端返回的受控错误（HTTP 400/403/404/409 等，body 形如 `{"error": "..."}`）。 */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// --------------------------------------------------------------------------
// 响应类型 —— 逐个对照 tools/wb_ui.py 的实际返回字段写，不凭印象
// --------------------------------------------------------------------------

export interface ProcessInfo {
  pid: number;
  cmd: string;
}

/** 一个客户端（WorkBuddy / WorkBuddy AI）的探测结果。 */
export interface ClientStatus {
  key: string;
  display: string;
  hint: string;
  running: boolean;
  processes: ProcessInfo[];
  home: string;
  home_confirmed: boolean;
  home_exists: boolean;
  db_exists: boolean;
  home_note: string;
}

export interface StateResponse {
  platform: string;
  state_dir: string;
  clients: ClientStatus[];
  /** 正在运行的客户端显示名，用于提示语。 */
  running_names: string[];
  /** 两个客户端是否都已退出 —— 执行写入的**唯一**前置条件。 */
  all_stopped: boolean;
  has_plan: boolean;
  last_run_code: number | null;
  autosync: unknown;
}

/** 计划生成时可选的迁移项。默认全开（与界面上的勾选一致），另有几项默认关。 */
export interface PlanOptions {
  include_changes?: boolean;
  include_skills?: boolean;
  include_plugins?: boolean;
  include_automations?: boolean;
  include_storage?: boolean;
  include_connectors?: boolean;
  include_claw?: boolean;
  include_memory?: boolean;
  overwrite_assets?: boolean;
}

export interface SessionSample {
  id: string | null;
  title: string;
  cwd: string | null;
  status: string | null;
}

/** `/api/plan` 的返回：计划的**摘要视图**，不是完整计划文档。 */
export interface PlanView {
  plan_id: string;
  created_at: string;
  homes: Record<string, { label: string; path: string; uid: string }>;
  options: Record<string, boolean>;
  summary: Record<string, unknown>;
  counts: Record<string, Record<string, number>>;
  sample: Record<string, SessionSample[]>;
}

/** `/api/verify`、`/api/backup` 这类「跑一段、回一段日志」的端点。 */
export interface RunResult {
  code: number;
  log: string;
}

/**
 * 流式端点的三种事件。
 *
 * 注意 `done` 里的 `code` 才是结论 —— 流正常读完**不等于**迁移成功，
 * 所以调用方必须看 `code`，不能拿"没抛异常"当成功。
 */
export type SseEvent =
  | { type: "start" }
  | { type: "log"; text: string }
  | { type: "done"; code: number; what: string };

// --------------------------------------------------------------------------
// 客户端
// --------------------------------------------------------------------------

const TOKEN_PARAM = "t";

/** 从当前页面 URL 取 token。没有就抛 —— 静默降级会变成「所有请求都 403」。 */
export function tokenFromLocation(search: string): string {
  const token = new URLSearchParams(search).get(TOKEN_PARAM);
  if (!token) {
    throw new ApiError("缺少访问凭据（URL 里的 t= 参数）。请用启动时打印的完整链接打开页面。");
  }
  return token;
}

export class BridgeClient {
  readonly base: string;
  readonly token: string;

  constructor(base: string, token: string) {
    // 去掉结尾斜杠，避免拼出 `//api/...` 这种路径。
    this.base = base.replace(/\/+$/, "");
    this.token = token;
  }

  /** 从当前页面构造客户端：同源 + URL 里的 token。 */
  static fromLocation(loc: { origin: string; search: string }): BridgeClient {
    return new BridgeClient(loc.origin, tokenFromLocation(loc.search));
  }

  private url(path: string, query: Record<string, string> = {}): string {
    const params = new URLSearchParams({ ...query, [TOKEN_PARAM]: this.token });
    return `${this.base}${path}?${params.toString()}`;
  }

  /**
   * 把非 2xx 响应转成带原文的 ApiError。
   *
   * 服务端的错误文案是给人看的（"WorkBuddy 仍在运行。请先完全退出客户端…"），
   * 所以这里要把它**原样**带出来，不能吞掉换成"请求失败"。
   */
  private async toError(res: Response): Promise<ApiError> {
    let detail = `HTTP ${res.status}`;
    try {
      const body: unknown = await res.json();
      if (body && typeof body === "object" && "error" in body) {
        const msg = (body as { error: unknown }).error;
        if (typeof msg === "string" && msg) detail = msg;
      }
    } catch {
      // body 不是 JSON（例如代理返回的 HTML）—— 保留 HTTP 状态码即可。
    }
    return new ApiError(detail, res.status);
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const res = await fetch(this.url(path), init);
    if (!res.ok) throw await this.toError(res);
    return (await res.json()) as T;
  }

  getState(): Promise<StateResponse> {
    return this.request<StateResponse>("GET", "/api/state");
  }

  survey(): Promise<unknown> {
    return this.request<unknown>("GET", "/api/survey");
  }

  accounts(days = 14, refresh = false): Promise<unknown> {
    const query: Record<string, string> = { days: String(days) };
    if (refresh) query["refresh"] = "1";
    return this.request<unknown>("GET", `/api/accounts?${new URLSearchParams(query).toString()}`);
  }

  plan(options: PlanOptions = {}): Promise<PlanView> {
    return this.request<PlanView>("POST", "/api/plan", { options });
  }

  verify(): Promise<RunResult> {
    return this.request<RunResult>("POST", "/api/verify");
  }

  restore(confirm?: string): Promise<RunResult> {
    return this.request<RunResult>("POST", "/api/restore", confirm ? { confirm } : {});
  }

  backup(body: Record<string, unknown> = {}): Promise<RunResult> {
    return this.request<RunResult>("POST", "/api/backup", body);
  }

  quitClients(opts: { allow_force?: boolean; wait?: number } = {}): Promise<unknown> {
    return this.request<unknown>("POST", "/api/quit-clients", opts);
  }

  /**
   * 执行迁移并逐条吐出进度。
   *
   * 用 `fetch` 手读 SSE 而不是 `EventSource`：EventSource 不能带自定义请求头，
   * 而且这里需要在调用方取消时能真正中断读取。手读的代价是二十来行解析。
   */
  async *streamApply(planId: string): AsyncGenerator<SseEvent> {
    const res = await fetch(this.url("/api/apply", { plan_id: planId }), {
      headers: { Accept: "text/event-stream" },
    });
    if (!res.ok) throw await this.toError(res);
    if (!res.body) throw new ApiError("服务端没有返回流式响应", res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE 以空行分隔事件；`data:` 行拼起来就是这一条的 JSON。
        let sep = buffer.indexOf("\n\n");
        while (sep !== -1) {
          const raw = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const data = raw
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice("data:".length).trimStart())
            .join("\n");
          if (data) yield JSON.parse(data) as SseEvent;
          sep = buffer.indexOf("\n\n");
        }
      }
    } finally {
      // 调用方提前 break（例如用户取消）时也要放掉底层连接。
      reader.releaseLock();
    }
  }

  /**
   * 把流跑完，返回结论码。
   *
   * 单独提供这个方法是为了把「流读完了」和「迁移成功了」区分开 ——
   * 前者不蕴含后者，把两者混为一谈会让失败静默通过。
   */
  async runApply(planId: string, onLog?: (text: string) => void): Promise<number> {
    let code = -1;
    for await (const event of this.streamApply(planId)) {
      if (event.type === "log") onLog?.(event.text);
      else if (event.type === "done") code = event.code;
    }
    return code;
  }
}
