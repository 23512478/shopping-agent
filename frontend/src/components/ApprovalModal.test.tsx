import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalModal, { PendingApproval } from './ApprovalModal'

const basePending: PendingApproval = {
  action: 'confirm_order',
  product: { product_id: 'p-001', title: '降噪耳机' },
  price: 1999,
  requested_at: '2026-10-10T12:00:00Z',
}

describe('ApprovalModal', () => {
  it('展示下单动作、商品和金额', () => {
    render(<ApprovalModal pending={basePending} submitting={false} onDecide={vi.fn()} />)
    expect(screen.getByText('确认下单')).toBeInTheDocument()
    expect(screen.getByText('降噪耳机')).toBeInTheDocument()
    expect(screen.getByText('¥ 1999')).toBeInTheDocument()
  })

  it('没商品显示占位文案，价格缺失显示价格未验证', () => {
    render(
      <ApprovalModal
        pending={{ action: 'confirm_order', product: null, price: null }}
        submitting={false}
        onDecide={vi.fn()}
      />,
    )
    expect(screen.getByText('未选出具体商品')).toBeInTheDocument()
    expect(screen.getByText('价格未验证')).toBeInTheDocument()
  })

  it('后端 pending 带的是 name 字段，弹窗优先显示商品名而不是 product_id', () => {
    render(
      <ApprovalModal
        pending={{
          action: 'confirm_order',
          product: {
            product_id: 'xiaomi-buds-4-pro',
            name: '小米 Buds 4 Pro 真无线降噪耳机',
            platform: '拼多多',
          },
          price: 799,
        }}
        submitting={false}
        onDecide={vi.fn()}
      />,
    )
    expect(screen.getByText('小米 Buds 4 Pro 真无线降噪耳机')).toBeInTheDocument()
    expect(screen.queryByText('xiaomi-buds-4-pro')).not.toBeInTheDocument()
  })

  it('点确认购买直接回调 approved=true，不弹原因框', async () => {
    const onDecide = vi.fn()
    const user = userEvent.setup()
    render(<ApprovalModal pending={basePending} submitting={false} onDecide={onDecide} />)

    await user.click(screen.getByRole('button', { name: '确认购买' }))

    expect(onDecide).toHaveBeenCalledTimes(1)
    expect(onDecide).toHaveBeenCalledWith(true)
  })

  it('取消先展开原因框，再想想能收起', async () => {
    const onDecide = vi.fn()
    const user = userEvent.setup()
    render(<ApprovalModal pending={basePending} submitting={false} onDecide={onDecide} />)

    await user.click(screen.getByRole('button', { name: '取消' }))
    const reasonBox = screen.getByPlaceholderText('不买的原因（可选），会告诉 AI')
    expect(reasonBox).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '再想想' }))
    expect(screen.queryByPlaceholderText('不买的原因（可选），会告诉 AI')).not.toBeInTheDocument()
    expect(onDecide).not.toHaveBeenCalled()
  })

  it('填了原因的取消，回传 approved=false 和原因', async () => {
    const onDecide = vi.fn()
    const user = userEvent.setup()
    render(<ApprovalModal pending={basePending} submitting={false} onDecide={onDecide} />)

    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.type(screen.getByPlaceholderText('不买的原因（可选），会告诉 AI'), '太贵了，不要')
    await user.click(screen.getByRole('button', { name: '确认取消' }))

    expect(onDecide).toHaveBeenCalledTimes(1)
    expect(onDecide).toHaveBeenCalledWith(false, '太贵了，不要')
  })

  it('空原因取消时不带 reason 参数', async () => {
    const onDecide = vi.fn()
    const user = userEvent.setup()
    render(<ApprovalModal pending={basePending} submitting={false} onDecide={onDecide} />)

    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: '确认取消' }))

    expect(onDecide).toHaveBeenCalledWith(false, undefined)
  })

  it('提交中按钮禁用、文案变成处理中', () => {
    render(<ApprovalModal pending={basePending} submitting={true} onDecide={vi.fn()} />)
    expect(screen.getByRole('button', { name: '处理中…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  })
})
