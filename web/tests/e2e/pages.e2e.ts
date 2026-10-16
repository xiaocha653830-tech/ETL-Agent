import { expect, test, type Page } from '@playwright/test'

const PASSWORD = 'Demo@12345'

async function signIn(page: Page, email: string) {
  await page.goto('/login')
  await page.getByLabel('邮箱').fill(email)
  await page.getByLabel('密码').fill(PASSWORD)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/dashboard/)
}

test('运行中心能列出运行并显示质量结论', async ({ page }) => {
  await signIn(page, 'maker@example.com')
  await page.getByRole('link', { name: '运行与审批' }).click()
  await page.getByRole('link', { name: '运行中心' }).click()

  await expect(page).toHaveURL(/\/runs/)
  await expect(page.getByRole('heading', { level: 1 })).toContainText('运行中心')

  // 库里已有成功运行，列表应当有内容而不是空态
  await expect(page.getByText('执行摘要')).toBeVisible()

  // 有质量契约的那次运行应当能看到分流数字；没有的则要显式说「没有质量契约」
  await expect(page.getByText(/有效行|本次运行没有质量契约/)).toBeVisible()
})

test('安全治理的两个 tab 都能打开', async ({ page }) => {
  await signIn(page, 'maker@example.com')

  await page.goto('/governance')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('安全治理')
  // 算子能力来自 M4 注册表，非空
  await expect(page.getByRole('heading', { name: '可用算子' })).toBeVisible()
  await expect(page.getByText('seatunnel.source.Jdbc').first()).toBeVisible()

  await page.getByRole('link', { name: '项目成员' }).click()
  await expect(page).toHaveURL(/tab=members/)
  await expect(page.getByRole('heading', { name: '项目成员' })).toBeVisible()
  // 断言行而不是某个具体邮箱：成员显示的是显示名，邮箱只是兜底
  await expect(page.locator('table tbody tr').first()).toBeVisible()
})

test('非操作员在运行中心看不到取消与清理按钮，只剩原因说明', async ({ page }) => {
  // 权限门在前端只是入口收敛，真正拦截在服务端；这里验的是**别让人对着按钮发问**
  await signIn(page, 'checker.data@example.com')
  await page.goto('/runs')

  await expect(page.getByRole('heading', { name: '运行操作' })).toBeVisible()
  await expect(page.getByRole('button', { name: '请求取消' })).toHaveCount(0)
  await expect(page.getByText('只有操作员可以请求取消运行。')).toBeVisible()
  // 这段文案在面板与确认框里各有一份（对话框复述一次原因），取第一处即可
  await expect(page.getByText('只有操作员可以清理运行残留。').first()).toBeVisible()
})

test('审计台对审计员角色可用（全局只读的归属页）', async ({ page }) => {
  await signIn(page, 'auditor@example.com')
  await page.goto('/audit')

  await expect(page.getByRole('heading', { level: 1 })).toContainText('审计台')
  await expect(page.getByRole('heading', { name: '链完整性' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '事件链' })).toBeVisible()
  // 审计面不得出现任何写操作
  await expect(page.getByRole('button', { name: /审批|提交|取消|清理|保存/ })).toHaveCount(0)
})
