import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dispatchRefresh, default as worker } from "../cloudflare/update-watchdog/worker.mjs";

test("backup dispatch requests a freshness-gated run", async () => {
  let request;
  await dispatchRefresh({ GITHUB_DISPATCH_TOKEN: "test-token" }, async (url, options) => {
    request = { url, options };
    return { status: 200 };
  });
  assert.match(request.url, /deploy-cloudflare-private\.yml\/dispatches$/);
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers.Authorization, "Bearer test-token");
  assert.deepEqual(JSON.parse(request.options.body), {
    ref: "main",
    inputs: { force_refresh: "false" },
  });
});

test("missing token never sends a request", async () => {
  await assert.rejects(dispatchRefresh({}, () => {
    throw new Error("unexpected network request");
  }), /not configured/);
});

test("GitHub rejection reports only the status", async () => {
  await assert.rejects(
    dispatchRefresh({ GITHUB_DISPATCH_TOKEN: "private-token" }, async () => ({ status: 403 })),
    (error) => error.message === "Workflow dispatch failed (HTTP 403)" &&
      !error.message.includes("private-token"),
  );
});

test("scheduled handler fails closed when the secret is missing", async () => {
  let pending;
  worker.scheduled({}, {}, {
    waitUntil(promise) { pending = promise; },
  });
  assert.equal(typeof pending?.then, "function");
  await assert.rejects(pending, /not configured/);
});

test("backup schedule and workflow input stay aligned", () => {
  const config = JSON.parse(readFileSync(
    new URL("../cloudflare/update-watchdog/wrangler.jsonc", import.meta.url), "utf8",
  ));
  assert.equal(config.workers_dev, false);
  assert.deepEqual(config.secrets.required, ["GITHUB_DISPATCH_TOKEN"]);
  assert.deepEqual(config.triggers.crons, ["30 9 * * 1-5", "30 12 * * 1-5"]);
  const workflow = readFileSync(
    new URL("../.github/workflows/deploy-cloudflare-private.yml", import.meta.url), "utf8",
  );
  assert.match(workflow, /force_refresh:\s*\n\s*description:.*\n\s*type: boolean\s*\n\s*default: true/);
  assert.match(workflow, /\[ "\$EVENT_NAME" = "workflow_dispatch" \] && \[ "\$FORCE_REFRESH" != "false" \]/);
});
