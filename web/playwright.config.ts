import { defineConfig } from '@playwright/test'

/**
 * 端到端冒烟。
 *
 * 与单元测试分目录：`tests/unit` 归 vitest（纯函数、node 环境），`tests/e2e` 归
 * Playwright（真实浏览器 + 真实后端）。两者用例后缀也不同，避免互相捡到对方的文件。
 *
 * **需要后端在运行**（默认 `http://127.0.0.1:8000`，Vite 代理 `/api` 转发过去）。
 * 后端没起时用例会因登录失败而挂——那是有意义的失败：这套用例存在的目的就是
 * 「前后端真的能对上」，用替身跑通它反而什么都证明不了。
 */
export default defineConfig({
  testDir: 'tests/e2e',
  testMatch: '**/*.e2e.ts',
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    // 走完整 Chromium 而不是 headless_shell：两者由不同的下载包提供，
    // 只装了其中一个时另一条路径会直接报「Executable doesn't exist」。
    channel: 'chromium',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: true,
    timeout: 60_000,
  },
})
