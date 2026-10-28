const DISPATCH_URL =
  "https://api.github.com/repos/yutaworld30-png/capital-gain-radar/actions/workflows/deploy-cloudflare-private.yml/dispatches";
const US_DISPATCH_URL =
  "https://api.github.com/repos/yutaworld30-png/capital-gain-radar/actions/workflows/update-us-market-private.yml/dispatches";
const US_CRON = "45 21 * * mon-fri";

export async function dispatchRefresh(env, fetcher = fetch, usMarket = false) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    throw new Error("GITHUB_DISPATCH_TOKEN is not configured");
  }

  const response = await fetcher(usMarket ? US_DISPATCH_URL : DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "capital-gain-radar-update-watchdog",
      "X-GitHub-Api-Version": "2026-03-10",
    },
    body: JSON.stringify(usMarket ? { ref: "main" } : {
      ref: "main", inputs: { force_refresh: "false" },
    }),
    signal: AbortSignal.timeout(10000),
  });
  if (response.status !== 200 && response.status !== 204) {
    // GitHub error bodies can contain request details; never log them or the token.
    throw new Error(`Workflow dispatch failed (HTTP ${response.status})`);
  }
  console.log(usMarket
    ? "US market refresh dispatched; the workflow will check the confirmed market session."
    : "Backup refresh dispatched; the workflow will check published data freshness.");
}

export default {
  scheduled(event, env, ctx) {
    const cron = String(event.cron || "").toLowerCase();
    if (cron !== US_CRON && cron !== "30 9 * * mon-fri" && cron !== "30 12 * * mon-fri") {
      throw new Error("Unknown watchdog schedule");
    }
    ctx.waitUntil(dispatchRefresh(env, fetch, cron === US_CRON));
  },
};
