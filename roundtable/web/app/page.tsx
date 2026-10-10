// M1 status page: shows API readiness and provider quotas. Agent lanes and the claims
// board arrive in M7. Rendered on the server so the API URL can stay internal.

export const dynamic = "force-dynamic";

type Check = {
  ok: boolean;
  required: boolean;
  configured: boolean;
  detail: string;
  latency_s: number | null;
  missing_models: string[];
};

type Ready = { status: "ok" | "degraded" | "fail"; checks: Record<string, Check> };

type Provider = {
  name: string;
  kind: string;
  usable: boolean;
  reason: string | null;
  required: boolean;
  rate_limit: { rpm: number | null; rpd: number | null };
  used_today: number;
  cooldown_s: number;
  models: string[];
};

const API = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T | string> {
  try {
    const res = await fetch(`${API}${path}`, { cache: "no-store", signal: AbortSignal.timeout(8000) });
    // /readyz answers 503 with a full report when a required dependency is down.
    if (!res.ok && res.status !== 503) return `HTTP ${res.status}`;
    return (await res.json()) as T;
  } catch (err) {
    return err instanceof Error ? err.message : String(err);
  }
}

const badge: Record<string, string> = {
  ok: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300",
  degraded: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300",
  fail: "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-300",
};

function Dot({ ok, off = false }: { ok: boolean; off?: boolean }) {
  const color = off ? "bg-zinc-400" : ok ? "bg-emerald-500" : "bg-red-500";
  return (
    <span
      aria-label={off ? "not configured" : ok ? "up" : "down"}
      className={`inline-block h-2.5 w-2.5 rounded-full ${color}`}
    />
  );
}

export default async function Home() {
  const [ready, providers] = await Promise.all([
    getJson<Ready>("/readyz"),
    getJson<Provider[]>("/v1/providers"),
  ]);

  return (
    <main className="mx-auto max-w-4xl px-4 py-10">
      <header className="mb-8 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Roundtable</h1>
          <p className="text-sm text-zinc-500">System status</p>
        </div>
        {typeof ready !== "string" && (
          <span className={`rounded-full px-3 py-1 text-sm font-medium ${badge[ready.status]}`}>
            {ready.status}
          </span>
        )}
      </header>

      <section className="mb-10">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">Dependencies</h2>
        {typeof ready === "string" ? (
          <p className="rounded-md border border-red-300 p-4 text-sm text-red-700 dark:border-red-800 dark:text-red-300">
            API unreachable at {API}: {ready}
          </p>
        ) : (
          <ul className="divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {Object.entries(ready.checks).map(([name, c]) => (
              <li key={name} className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:gap-4">
                <div className="flex min-w-48 items-center gap-2 font-medium">
                  <Dot ok={c.ok} off={!c.configured} />
                  {name}
                  {c.required && <span className="text-xs font-normal text-zinc-500">required</span>}
                </div>
                <div className="flex-1 break-words text-sm text-zinc-600 dark:text-zinc-400">
                  {c.detail}
                  {c.missing_models.length > 0 && (
                    <span className="block text-amber-700 dark:text-amber-400">
                      not pulled yet: {c.missing_models.join(", ")}
                    </span>
                  )}
                </div>
                {c.latency_s !== null && (
                  <div className="text-xs tabular-nums text-zinc-500">{Math.round(c.latency_s * 1000)} ms</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">Providers</h2>
        {typeof providers === "string" ? (
          <p className="text-sm text-zinc-500">Unavailable: {providers}</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-zinc-200 dark:border-zinc-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-zinc-50 text-xs uppercase text-zinc-500 dark:bg-zinc-900">
                <tr>
                  <th className="px-4 py-2 font-medium">Provider</th>
                  <th className="px-4 py-2 font-medium">Models</th>
                  <th className="px-4 py-2 font-medium">Limits</th>
                  <th className="px-4 py-2 font-medium">Used today</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {providers.map((p) => (
                  <tr key={p.name}>
                    <td className="px-4 py-2 align-top">
                      <div className="flex items-center gap-2 font-medium">
                        <Dot ok={p.usable} off={!p.usable && !p.required} />
                        {p.name}
                      </div>
                      <div className="text-xs text-zinc-500">{p.reason ?? p.kind}</div>
                    </td>
                    <td className="px-4 py-2 align-top font-mono text-xs">
                      {p.models.length ? p.models.map((m) => <div key={m}>{m}</div>) : "–"}
                    </td>
                    <td className="px-4 py-2 align-top tabular-nums">
                      {p.rate_limit.rpm ?? "∞"}/min · {p.rate_limit.rpd ?? "∞"}/day
                    </td>
                    <td className="px-4 py-2 align-top tabular-nums">
                      {p.used_today}
                      {p.rate_limit.rpd ? ` / ${p.rate_limit.rpd}` : ""}
                      {p.cooldown_s > 0 && (
                        <div className="text-xs text-amber-700 dark:text-amber-400">cooling down {p.cooldown_s}s</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}
