import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

declare global {
  interface Window {
    setWorkspace(name: string): void;
    WF: {
      steps: Record<string, unknown>;
      openWorkflow(workflow: unknown): void;
    };
    BGState: { push(state: Record<string, unknown>): void };
  }
}

const seat = {
  role: "art", title: "Art", mission: "Build production-ready visual assets.",
  enabled: true, write_globs: ["assets/**"],
};

async function mockProject(page: Page, extra: Record<string, unknown> = {}) {
  await page.route("**/api/state", (route) => route.fulfill({ json: {
    project: { name: "Browser smoke", engine: "godot", dimension: "2d" },
    root: "/tmp/browser-smoke", seats: [seat], queue: [], sessions: [],
    asset_groups: [], assets: [], verify: {}, lore: { canon: [] }, ...extra,
  } }));
}

test("a structured seat brief reaches queue and dispatch", async ({ page }) => {
  await mockProject(page);
  let filed: Record<string, unknown> = {};
  await page.route("**/api/queue", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    filed = route.request().postDataJSON();
    await route.fulfill({ json: { id: 901, status: "queued" } });
  });
  await page.route("**/api/queue/901/dispatch", (route) =>
    route.fulfill({ json: { ok: true, data: { id: 901, status: "dispatched" } } }));

  await page.goto("/");
  await page.evaluate(() => window.setWorkspace("seats"));
  await expect(page.locator(".bgs-brief")).toBeVisible();
  const fields = page.locator(".bgs-brief-field textarea");
  await fields.nth(0).fill("Build the inventory overlay");
  await fields.nth(1).fill("Touch only the HUD scene");
  await fields.nth(2).fill("Screenshot at 1280x720 with no overlap");
  await page.getByRole("button", { name: "Start now" }).click();

  await expect.poll(() => filed.brief).toContain("Acceptance");
  expect(filed).toMatchObject({ seat: expect.any(String), source: "seat-desk" });
});

test("Studio preflights and runs a direct tool graph", async ({ page }) => {
  await mockProject(page);
  let plan: Record<string, unknown> = {};
  await page.route("**/api/workflows/preflight", async (route) => {
    plan = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true, data: {
      ok: true, errors: [], nodes: 2, tools: [{ ok: true }], generators: [],
      estimate_usd: 0,
    } } });
  });
  await page.route("**/api/workflows/runs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({ json: { ok: true, data: {
      id: 77, name: "Smoke graph", status: "passed", counts: { passed: 2 },
      nodes: [
        { node_id: "brief", kind: "passive", label: "Brief", status: "passed", output: { text: "status" } },
        { node_id: "status", kind: "tool", label: "Godot status", status: "passed", output: { data: { available: true } } },
      ],
    } } });
  });

  await page.goto("/");
  await page.evaluate(() => window.setWorkspace("studio"));
  await expect.poll(() => page.evaluate(() => !!window.WF.steps["tool.godot.status"])).toBeTruthy();
  await expect(page.getByRole("button", { name: /Model comparison/ })).toBeVisible();
  await page.evaluate(() => window.WF.openWorkflow({
    id: "e2e-graph", name: "Smoke graph", category: "custom",
    nodes: [
      { id: "brief", type: "input.task", x: 80, y: 100, config: { text: "Check this project" } },
      { id: "status", type: "tool.godot.status", x: 360, y: 100, config: {} },
    ],
    edges: [{ from: ["brief", "o"], to: ["status", "in"] }],
  }));
  await expect(page.locator(".wf-build")).toBeVisible();
  await page.getByRole("button", { name: "Run workflow" }).click();

  await expect(page.locator("#wf-runbar")).toContainText("passed");
  expect((plan.nodes as Array<{ kind: string }>).some((node) => node.kind === "agent")).toBeFalsy();
});

test("lazy screens load their chunks from the static mount", async ({ page }) => {
  await mockProject(page);
  const brokenChunkUrls: string[] = [];
  const loadedChunks = new Set<string>();
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/static/dist/bgate-") && path.endsWith(".js")
        && response.ok()) loadedChunks.add(path);
    if ((path.startsWith("/dist/") || path.includes("/static/dist/dist/"))
        && response.status() >= 400) brokenChunkUrls.push(path);
  });

  await page.goto("/");
  await expect.poll(() => loadedChunks.size).toBeGreaterThanOrEqual(9);
  const affected = { agents: ".bg4-console", brainstorm: ".bg4-room", settings: ".bg4-settings" };
  for (const [key, selector] of Object.entries(affected)) {
    await page.evaluate((workspace) => window.setWorkspace(workspace), key);
    const island = page.locator(`[data-react="${key}"]`);
    await expect(island).not.toContainText("This screen could not load");
    await expect(island.locator(selector)).toBeVisible();
  }
  for (const chunk of ["Overview", "Assets", "World", "Playtests", "Floor2",
                       "Agents", "Room", "Settings", "Seats"]) {
    expect(loadedChunks).toContain(`/static/dist/bgate-${chunk}.js`);
  }
  expect(brokenChunkUrls).toEqual([]);
  expect(pageErrors).toEqual([]);
});

test("Assets works as a review, library, and integrity desk", async ({ page }) => {
  const artifact = { id: 41, logical_name: "hero", path: "art/hero.png", kind: "image",
    status: "candidate", revision: 2 };
  await mockProject(page, {
    asset_groups: [{ logical_name: "hero", approved: null, candidates: [artifact],
      revisions: [artifact], feedback: [] }],
    assets: [{ path: "art/hero.png", kind: "image", bytes: 2048 }],
    verify: { counts: { modified: 1, missing: 0, pending: 0 },
      modified: [{ path: "art/hero.png" }] },
  });

  await page.goto("/");
  await page.evaluate((state) => window.BGState.push(state), {
    project: { name: "Browser smoke", engine: "godot", dimension: "2d" },
    root: "/tmp/browser-smoke", sessions: [], controls: [], lore: { canon: [] },
    asset_groups: [{ logical_name: "hero", approved: null, candidates: [artifact],
      revisions: [artifact], feedback: [] }],
    assets: [{ path: "art/hero.png", kind: "image", bytes: 2048 }],
    verify: { counts: { modified: 1, missing: 0, pending: 0 },
      modified: [{ path: "art/hero.png" }] },
  });
  await page.evaluate(() => window.setWorkspace("assets"));
  await expect(page.locator(".asset-desk")).toBeVisible();
  await page.getByRole("button", { name: /hero/i }).click();
  await expect(page.locator(".asset-inspector")).toContainText("hero");
  const modes = page.getByRole("navigation", { name: "Asset workspace" });
  await modes.getByRole("button", { name: /Library/ }).click();
  await expect(page.locator("#asset-lib-root")).toBeVisible();
  await modes.getByRole("button", { name: /Integrity/ }).click();
  await expect(page.locator(".asset-integrity")).toContainText("1 files need attention");
});
