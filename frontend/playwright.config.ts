import { defineConfig, devices } from "@playwright/test";

const python = process.env.BGATE_E2E_PYTHON || "python";

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "line",
  use: { baseURL: "http://127.0.0.1:7791", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: {
    ...devices["Desktop Chrome"],
    channel: process.env.CI ? undefined : "chrome",
  } }],
  webServer: {
    command: `"${python}" e2e/server.py`,
    cwd: ".",
    url: "http://127.0.0.1:7791/api/state",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
