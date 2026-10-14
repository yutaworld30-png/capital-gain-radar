const DISPATCH_URL =
  "https://api.github.com/repos/yutaworld30-png/capital-gain-radar/actions/workflows/deploy-cloudflare-private.yml/dispatches";

export async function dispatchRefresh(env, fetcher = fetch) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    throw new Error("GITHUB_DISPATCH_TOKEN is not configured");
  }

  const response = await fetcher(DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "capital-gain-radar-update-watchdog",
      "X-GitHub-Api-Version": "2026-03-10",
    },
    body: JSON.stringify({ ref: "main", inputs: { force_refresh: "false" } }),
    signal: AbortSignal.timeout(10000),
  });
  if (response.status !== 200 && response.status !== 204) {
    // GitHub error bodies can contain request details; never log them or the token.
    throw new Error(`Workflow dispatch failed (HTTP ${response.status})`);
  }
  console.log("Backup refresh dispatched; the workflow will check published data freshness.");
}

export default {
  scheduled(_event, env, ctx) {
    ctx.waitUntil(dispatchRefresh(env));
  },
};
