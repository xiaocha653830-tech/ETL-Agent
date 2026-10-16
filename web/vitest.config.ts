import { defineConfig } from 'vitest/config'

/**
 * 单元测试只覆盖**纯函数**（权限判定、展示格式化、查询串序列化）——它们零依赖、
 * 零 DOM，性价比最高，也正是「前端与服务端校验口径是否一致」最容易出错的地方。
 * 需要浏览器的部分交给 Playwright（见 `playwright.config.ts`）。
 *
 * `environment: 'node'` 是刻意的：这些模块在函数体内才碰 `window` / `document`，
 * 模块顶层没有副作用，所以不需要 jsdom。
 */
export default defineConfig({
  test: {
    include: ['tests/unit/**/*.spec.ts'],
    environment: 'node',
  },
})
