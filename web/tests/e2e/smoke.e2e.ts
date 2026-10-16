import { expect, test } from '@playwright/test'

/**
 * 端到端冒烟：**真实浏览器 + 真实后端**。
 *
 * 这里刻意不做接口替身。前端此前的问题恰恰是「契约层看着都对、但整条路径从未
 * 被走过」——用替身把它跑绿，等于把同一类风险再藏一遍。
 */
const EMAIL = 'maker@example.com'
const PASSWORD = 'Demo@12345'

test('未登录访问受保护页面会被送回登录页', async ({ page }) => {
  await page.goto('/runs')
  await expect(page).toHaveURL(/\/login/)
})

test('登录后进入总览，且侧边栏五个入口齐全', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('邮箱').fill(EMAIL)
  await page.getByLabel('密码').fill(PASSWORD)
  await page.getByRole('button', { name: '登录' }).click()

  await expect(page).toHaveURL(/\/dashboard/)
  await expect(page.getByRole('heading', { level: 1 })).toContainText('项目总览')

  // 「运行与审批」「安全治理」「审计台」此前都是死链——这一步就是在守它们
  for (const label of ['工作台', 'Pipeline Studio', '运行与审批', '安全治理', '审计台']) {
    await expect(page.getByRole('link', { name: label })).toBeVisible()
  }
})

test('审计台可达且链状态有明确措辞', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('邮箱').fill(EMAIL)
  await page.getByLabel('密码').fill(PASSWORD)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/dashboard/)

  await page.getByRole('link', { name: '审计台' }).click()
  await expect(page).toHaveURL(/\/audit/)
  await expect(page.getByRole('heading', { level: 1 })).toContainText('审计台')
  // 四种链状态之一，且不能把「尚无记录」说成「校验通过」
  await expect(page.getByText(/校验通过|尚无记录|校验不通过|无法完整验证/)).toBeVisible()
})
