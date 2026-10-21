// 导航用例：C 端只有五个入口（含盯价），失败任务（死信）不进任何 C 端导航。
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import NavBar from './NavBar'

describe('NavBar', () => {
  it('桌面导航 + 移动 tabbar 都是五个 C 端入口', () => {
    const { container } = render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>,
    )

    for (const label of ['首页', 'AI导购', '订单', '盯价', '我的']) {
      // 顶部 topnav 与底部 tabbar 各渲染一份
      expect(screen.getAllByText(label)).toHaveLength(2)
    }
    expect(container.querySelectorAll('.topnav-link')).toHaveLength(5)
    expect(container.querySelectorAll('.tabbar-link')).toHaveLength(5)
  })

  it('不暴露失败任务/死信入口', () => {
    render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>,
    )
    expect(screen.queryByText(/失败任务/)).not.toBeInTheDocument()
    expect(screen.queryByText(/死信/)).not.toBeInTheDocument()
    expect(screen.queryByText(/admin/i)).not.toBeInTheDocument()
  })
})