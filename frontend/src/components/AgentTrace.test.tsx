import { afterEach, describe, expect, it, vi } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AgentTrace from './AgentTrace'

interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
}

// 把帧数组编成 SSE 流（data: {...}\n\n），模拟后端推送
function sseResponse(frames: unknown[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`))
      }
      controller.close()
    },
  })
  return new Response(body)
}

let historyPayload: { messages: HistoryMessage[] }
let notifyPayload: { notifications: unknown[] } = { notifications: [] }
let pendingPayload: Record<string, unknown> | null = null
let chatBody: { message: string; session_id: string } | undefined
let approvalBody: { approved: boolean; reason?: string } | undefined

function installFetch(chatFrames: unknown[], approvalFrames: unknown[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/notify')) {
      return new Response(JSON.stringify(notifyPayload))
    }
    if (url.includes('/api/sessions/')) {
      return new Response(JSON.stringify(historyPayload))
    }
    if (url.includes('/api/chat/stream')) {
      chatBody = JSON.parse(String(init?.body))
      return sseResponse(chatFrames)
    }
    if (url.includes('/api/approval/')) {
      // GET 是刷新后找回挂起的审批，POST 才是提交审批决定
      if (init?.method === 'POST') {
        approvalBody = JSON.parse(String(init?.body))
        return sseResponse(approvalFrames)
      }
      return new Response(JSON.stringify({ pending: pendingPayload }))
    }
    throw new Error(`测试没覆盖到的请求：${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
}

afterEach(() => {
  vi.unstubAllGlobals()
  pendingPayload = null
  notifyPayload = { notifications: [] }
})

const interruptFrame = {
  __interrupt__: [
    {
      value: {
        action: 'confirm_order',
        product: { product_id: 'p-001', title: '降噪耳机' },
        price: 1999,
      },
    },
  ],
}

// 弹窗可能在流还没读完时就挂出来了，等按钮能点再操作，避免竞态
async function awaitReadyButton(name: string) {
  await waitFor(() => expect(screen.getByRole('button', { name })).toBeEnabled())
}

describe('AgentTrace', () => {
  it('挂载时拉历史，助手 JSON 消息翻译成人话', async () => {
    historyPayload = {
      messages: [
        { role: 'user', content: '找个耳机' },
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'compare',
            comparison_table: [
              { product_id: 'p-001', platform: '京东', price: 1999, name: '降噪耳机' },
              { product_id: 'p-001', platform: '拼多多', price: 1899, name: '降噪耳机' },
            ],
            selected_product: {
              product_id: 'p-001',
              platform: '拼多多',
              price: 1899,
              name: '降噪耳机',
            },
            risk_report: {
              'p-001': {
                product_id: 'p-001',
                negative_rate: 0.03,
                shop_score: 4.7,
                risk_assessed: true,
              },
            },
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText('找个耳机')).toBeInTheDocument()
    // 意图标签是内部路由结果，不该出现在用户看到的气泡里
    expect(screen.queryByText(/意图/)).not.toBeInTheDocument()
    expect(screen.getByText(/帮你找到 1 件商品/)).toBeInTheDocument()
    expect(screen.getByText(/推荐：降噪耳机，拼多多 最低 ¥1899/)).toBeInTheDocument()
    expect(screen.getByText(/差评率 3.0%，店铺 4.7 分/)).toBeInTheDocument()
  })

  it('降价提醒卡片显示商品名，不拿内部商品 id 糊弄用户', async () => {
    historyPayload = { messages: [] }
    notifyPayload = {
      notifications: [
        {
          type: 'price_drop',
          product_id: 'jd-1261881',
          platform: '京东',
          price: 9.8,
          threshold: 300,
          name: '纽曼 NM-LK06 有线耳机',
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s-notify" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/降价提醒/)).toBeInTheDocument()
    expect(screen.getByText(/纽曼 NM-LK06 有线耳机/)).toBeInTheDocument()
    // 商品 id 是给机器看的，不该出现在卡片上
    expect(screen.queryByText(/jd-1261881/)).not.toBeInTheDocument()
  })

  it('开发模式 effect 双执行时，提醒不会被丢掉（接口读完即清空，丢了就没了）', async () => {
    historyPayload = { messages: [] }
    // 服务端是「读完即清空」：同一次挂载里只有第一个请求能拿到数据
    let served = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/notify')) {
        const body = served
          ? { notifications: [] }
          : {
              notifications: [
                {
                  type: 'price_drop',
                  product_id: 'jd-1261881',
                  platform: '京东',
                  price: 9.8,
                  threshold: 300,
                  name: '纽曼 NM-LK06 有线耳机',
                },
              ],
            }
        served = true
        return new Response(JSON.stringify(body))
      }
      if (url.includes('/api/sessions/')) return new Response(JSON.stringify(historyPayload))
      return new Response(JSON.stringify({ pending: null }))
    })
    vi.stubGlobal('fetch', fetchMock)

    render(
      <StrictMode>
        <AgentTrace sessionId="s-strict" onHistoryChange={vi.fn()} />
      </StrictMode>,
    )

    expect(await screen.findByText(/纽曼 NM-LK06 有线耳机/)).toBeInTheDocument()
  })

  it('搜空时诚实提示，不编造商品', async () => {
    historyPayload = {
      messages: [
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'search',
            product_ids: [],
            search_empty: true,
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/没找到符合条件的商品/)).toBeInTheDocument()
    expect(screen.queryByText(/意图/)).not.toBeInTheDocument()
  })

  it('「下单第一个」但会话里没搜到过商品：如实说指代落空，不装作在走下单', async () => {
    historyPayload = {
      messages: [
        { role: 'assistant', content: JSON.stringify({ intent: 'search', ref_unresolved: true }) },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/这个会话里我还没搜到过商品/)).toBeInTheDocument()
  })

  it('说了下单但没选出商品：气泡里说明白，不弹空确认框', async () => {
    historyPayload = {
      messages: [
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'search',
            product_ids: [],
            search_empty: true,
            order_skipped: 'no_product',
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    // order_skipped 优先于 search_empty：得先说清「下单这事没成」，再说搜空
    expect(await screen.findByText(/没选出能下单的商品/)).toBeInTheDocument()
  })

  it('快照里凑不出人话时兜一句提示，不把内部 JSON 甩给用户', async () => {
    historyPayload = {
      messages: [{ role: 'assistant', content: JSON.stringify({ intent: 'unknown' }) }],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/这句我还没太听懂/)).toBeInTheDocument()
    expect(screen.queryByText(/intent/)).not.toBeInTheDocument()
  })

  it('盯价注册成功后说清盯了几件、降到多少提醒，不只甩一句「盯价」', async () => {
    historyPayload = {
      messages: [
        { role: 'user', content: '帮我盯着这个耳机，降到 300 告诉我' },
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'monitor',
            product_ids: ['p-001', 'p-002'],
            monitor_config: { product_ids: ['p-001', 'p-002'], threshold: 300, active: true },
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/已开始盯价：这 2 件商品，降到 ¥300 就提醒你/)).toBeInTheDocument()
  })

  it('没说目标价时如实讲清楚「现在不会推提醒」，不装作已经在盯', async () => {
    historyPayload = {
      messages: [
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'monitor',
            product_ids: ['p-001'],
            monitor_config: { product_ids: ['p-001'], threshold: null, active: true },
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/已盯上这 1 件商品，但你没说降到多少提醒你/)).toBeInTheDocument()
    expect(screen.getByText(/降到 800 告诉我/)).toBeInTheDocument()
  })

  it('按百分比盯价（「降价 10% 提醒我」）时气泡说清降幅，不再说「你没说降到多少」', async () => {
    historyPayload = {
      messages: [
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'monitor',
            product_ids: ['p-001', 'p-002'],
            monitor_config: {
              product_ids: ['p-001', 'p-002'],
              threshold: null,
              drop_percent: 10,
              active: true,
            },
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/已开始盯价：这 2 件商品，比现在降 10% 就提醒你/)).toBeInTheDocument()
  })

  it('盯价没注册上（调度器没起）时说没注册上，不谎报在盯', async () => {
    historyPayload = {
      messages: [
        {
          role: 'assistant',
          content: JSON.stringify({
            intent: 'monitor',
            product_ids: ['p-001'],
            monitor_config: { product_ids: ['p-001'], threshold: 300, active: false },
          }),
        },
      ],
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/这 1 件商品的盯价没注册上/)).toBeInTheDocument()
  })

  it('发送后内部节点不进气泡，挂起帧自动弹审批窗', async () => {
    historyPayload = { messages: [] }
    installFetch(
      [
        { __meta__: { session_id: 's1' } },
        { supervisor: { intent: 'search' } },
        { search: { product_ids: ['p-001', 'p-002'] } },
        interruptFrame,
      ],
      [],
    )
    const user = userEvent.setup()
    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    await user.type(await screen.findByRole('textbox'), '找个耳机然后下单')
    await user.click(screen.getByRole('button', { name: '发送' }))

    expect(chatBody).toEqual({ message: '找个耳机然后下单', session_id: 's1' })
    expect(await screen.findByText('确认下单')).toBeInTheDocument()
    expect(screen.getByText('降噪耳机')).toBeInTheDocument()
    // 后端推的节点名、节点计数、原始 JSON 都是内部实现，用户侧一个都不该看到
    expect(screen.queryByText(/本轮经过/)).not.toBeInTheDocument()
    expect(screen.queryByText('supervisor')).not.toBeInTheDocument()
    expect(screen.queryByText(/展开调试详情/)).not.toBeInTheDocument()
  })

  it('审批通过后回传 approved=true，弹窗关闭，归档节点名不外露', async () => {
    historyPayload = { messages: [] }
    installFetch(
      [{ __meta__: { session_id: 's1' } }, interruptFrame],
      [{ approval: { order_confirmed: true } }, { archive: { archived: true } }],
    )
    const user = userEvent.setup()
    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    await user.type(await screen.findByRole('textbox'), '找个耳机然后下单')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByText('确认下单')

    await awaitReadyButton('确认购买')
    await user.click(screen.getByRole('button', { name: '确认购买' }))

    expect(approvalBody).toEqual({ approved: true })
    await waitFor(() => expect(screen.queryByText('确认下单')).not.toBeInTheDocument())
    expect(screen.queryByText('archive')).not.toBeInTheDocument()
  })

  it('审批驳回回传原因，不出归档节点，历史气泡显示已驳回', async () => {
    historyPayload = { messages: [] }
    installFetch(
      [{ __meta__: { session_id: 's1' } }, interruptFrame],
      [{ approval: { order_confirmed: false } }],
    )
    const user = userEvent.setup()
    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    await user.type(await screen.findByRole('textbox'), '找个耳机然后下单')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByText('确认下单')

    await awaitReadyButton('取消')
    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.type(
      screen.getByPlaceholderText('不买的原因（可选），会告诉 AI'),
      '太贵了，不要',
    )
    // 驳回后后端历史里会出现带 order_confirmed:false 的助手消息
    historyPayload = {
      messages: [
        { role: 'user', content: '找个耳机然后下单' },
        {
          role: 'assistant',
          content: JSON.stringify({ intent: 'search', order_confirmed: false }),
        },
      ],
    }
    await user.click(screen.getByRole('button', { name: '确认取消' }))

    expect(approvalBody).toEqual({ approved: false, reason: '太贵了，不要' })
    // 驳回后的助手快照翻成人话：已取消下单
    expect(await screen.findByText(/已取消下单/)).toBeInTheDocument()
    expect(screen.queryByText('archive')).not.toBeInTheDocument()
    expect(screen.queryByText('确认下单')).not.toBeInTheDocument()
  })

  it('收到 duplicate 帧时提示别重复发送', async () => {
    historyPayload = { messages: [] }
    installFetch([{ duplicate: true }], [])
    const user = userEvent.setup()
    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    await user.type(await screen.findByRole('textbox'), '找个耳机')
    await user.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('这条消息刚刚发过，别着急再点一次')).toBeInTheDocument()
  })

  it('刷新后从后端找回卡在审批点的弹窗', async () => {
    historyPayload = { messages: [] }
    pendingPayload = {
      action: 'confirm_order',
      product: { product_id: 'p-001', title: '降噪耳机' },
      price: 1999,
    }
    installFetch([], [])

    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText('确认下单')).toBeInTheDocument()
    expect(screen.getByText('降噪耳机')).toBeInTheDocument()
    expect(screen.getByText('¥ 1999')).toBeInTheDocument()
  })

  it('轮询降价提醒后展示降价卡片', async () => {
    historyPayload = { messages: [] }
    notifyPayload = {
      notifications: [
        {
          type: 'price_drop',
          product_id: 'p-001',
          platform: 'jd',
          price: 899,
          threshold: 900,
        },
      ],
    }
    installFetch([], [])
    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/降价提醒/)).toBeInTheDocument()
    expect(screen.getByText(/p-001/)).toBeInTheDocument()
    expect(screen.getByText(/899/)).toBeInTheDocument()
  })

  it('按百分比盯的提醒卡片显示降幅，不显示空阈值', async () => {
    historyPayload = { messages: [] }
    notifyPayload = {
      notifications: [
        {
          type: 'price_drop',
          product_id: 'p-002',
          platform: 'jd',
          price: 899,
          threshold: null,
          drop_percent: 10,
        },
      ],
    }
    installFetch([], [])
    render(<AgentTrace sessionId="s1" onHistoryChange={vi.fn()} />)

    expect(await screen.findByText(/比之前降了 10% 起/)).toBeInTheDocument()
    expect(screen.queryByText(/阈值/)).not.toBeInTheDocument()
  })
})
