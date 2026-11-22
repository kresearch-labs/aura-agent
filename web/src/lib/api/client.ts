// client.ts — all data to the team hub (against OUR routes).

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(message: string, code: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export async function call<T>(path: string, body?: unknown,
                              method?: string, id?: number): Promise<T> {
  return raw<T>(path, body, method, id, 0);
}

// 429 obedience lives HERE, not at every caller: a GET that rates-limits
// waits out the hub's window (Retry-After, floor 1 s) and tries again —
// bounded, so a wedged hub still surfaces an error eventually.
async function raw<T>(path: string, body?: unknown,
                      method?: string, id?: number, hop = 0): Promise<T> {
  const r = await fetch(`/api/v1${path}`, {
    method: body === undefined ? method ?? "GET" : method ?? "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    // "same-origin" COULD hold cookies back on the LAN-only self-signed
    // ip-literal hub (chromium's own site-trust boundary; hub never ships
    // cross-origin answers so include is as strict, and always rides)
    credentials: "include",
  });
  const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
  if (r.status === 429 && (method ?? (body === undefined ? "GET" : "POST")) === "GET"
      && hop < 3) {
    const ra = Number(r.headers.get("Retry-After"));
    await new Promise((res) => setTimeout(res, Math.max(Number.isFinite(ra) ? ra * 1000 : 0, 1000)));
    return raw<T>(path, body, method, id, hop + 1);
  }
  if (!r.ok) {
    const e = (data as { error?: { message?: string; code?: string } }).error ?? {};
    throw new ApiError(e.message ?? `hub answered ${r.status}`,
                       e.code ?? "error", r.status);
  }
  return data as T;
}

export async function signIn(name: string, password: string): Promise<void> {
  await call("/signin", { name, password });   // cookie set by the server
}

export async function signOut(): Promise<void> {
  await call("/signout", {});                  // cookie cleared cookie-side too
}

export function connect(onEvent: (type: string, data: unknown, id: number) => void,
                        kinds?: string[]) {
  // Own stream address (an old `/events/stream` answer is a 404). EventSource lands on the
  // same-origin cookie lane.
  const es = new EventSource("/api/v1/stream");
  const names =
    kinds ?? ["task", "comment", "log", "chat", "mention", "typing", "read",
              "presence", "people", "project", "map", "reset"];
  for (const kind of names) {
    es.addEventListener(kind, (e: MessageEvent) =>
      onEvent(kind, JSON.parse(e.data), Number(e.lastEventId)));
  }
  return es;
}
