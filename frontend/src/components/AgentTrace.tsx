import { useEffect, useMemo, useState } from 'react'
import ApprovalModal, { PendingApproval } from './ApprovalModal'
import ProductCard from './ProductCard'
import { authHeaders } from '../api'
import { useProducts } from '../catalog'
import type { Product } from '../catalog'

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

type SseFrame = {
  __meta__?: { session_id?: string; note?: string | null }
  __interrupt__?: Array<{ value: PendingApproval }>
  duplicate?: boolean
} & Record<string, unknown>

interface Props {
  sessionId: string
  onHistoryChange: () => void
  /** 点商品卡片跳详情；不传就只展示不跳（测试里不传） */
  onOpenProduct?: (productId: string) => void
  /** 确认下单后跳「我的订单」；不传就留在对话里（测试里不传） */
  onOrderConfirmed?: () => void
}

interface PriceNotify {
  type: string
  product_id?: string
  platform?: string
  price?: number
  threshold?: number
  /** 按百分比盯的降幅：有值就显示「降价 X% 触发」，没有才显示绝对值阈值 */
  drop_percent?: number | null
  /** 触发时的基准价（盯着的那条线是怎么来的，卡片上说清楚） */
  prev_price?: number | null
  /** 商品名，查价结果里带着的；老数据没有就退回显示商品 id */
  name?: string
}

// 商品名动辄四五十字，卡片里只留前 20 个字，完整名字挂在 title 上
function notifyName(n: PriceNotify): string {
  const name = n.name || n.product_id || '商品'
  return name.length > 20 ? `${name.slice(0, 20)}…` : name
}

// 后端推过来的节点帧只用来更新一句「正在做什么」。节点名、意图标签、原始 JSON
// 都是系统内部的东西，用户看到的应该是一个正在干活的状态，不是一条流水线
const PHASE_TEXT: Record<string, string> = {
  supervisor: '正在理解你的需求…',
  search: '正在搜货…',
  compare: '正在比价…',
  risk: '正在排查风险…',
  prepare_order: '正在准备下单…',
  approval: '等你确认下单',
  archive: '正在归档…',
  monitor: '正在给你盯价…',
}

interface PriceRow {
  product_id: string
  platform: string
  price: number | null
  name?: string
  price_verified?: boolean
}
interface RiskRow {
  product_id: string
  negative_rate: number | null
  shop_score?: number | null
  shop_score_source?: 'user_evaluate' | 'shop_level' | null
  risk_assessed?: boolean
}
interface Snapshot {
  intent?: string
  product_ids?: string[]
  search_empty?: boolean
  comparison_table?: PriceRow[]
  selected_product?: PriceRow | null
  risk_report?: Record<string, RiskRow>
  cheapest?: string | null
  order_confirmed?: boolean | null
  /** 说了下单但一件商品都没选出来（no_product）：这时不弹确认框，只在气泡里如实讲 */
  order_skipped?: string | null
  /** 说了「下单第一个」但会话里从没搜到过商品：指代落空 */
  ref_unresolved?: boolean
  /** 盯价注册结果：盯了几件、阈值多少、调度任务有没有真起来 */
  monitor_config?: {
    product_ids?: string[]
    threshold?: number | null
    /** 按百分比盯的降幅（「降价 10% 提醒我」→ 10），和 threshold 二选一 */
    drop_percent?: number | null
    active?: boolean
    // 目标价已经被现价满足的件数（这些按现价当基准盯，再降就叫）
    below?: number
  }
}

// 把 assistant 的 state 快照翻成用户能看懂的话
function riskText(r: RiskRow): string {
  const nr = r.negative_rate != null ? `差评率 ${(r.negative_rate * 100).toFixed(1)}%` : '差评率未知'
  const ss =
    r.shop_score != null
      ? `${r.shop_score_source === 'shop_level' ? '店铺等级' : '店铺'} ${r.shop_score.toFixed(1)} 分`
      : ''
  return [nr, ss].filter(Boolean).join('，')
}

function assistantText(raw: string): string {
  let o: Snapshot
  try {
    o = JSON.parse(raw) as Snapshot
  } catch {
    return raw
  }
  const parts: string[] = []

  // 盯价：说清到底盯上没有、盯了几件、降到多少才提醒。
  // 不说的话气泡只有干巴巴一句「盯价」，用户根本不知道注册成功没有。
  if (o.intent === 'monitor') {
    const cfg = o.monitor_config
    const ids = cfg?.product_ids ?? o.product_ids ?? []
    if (!ids.length) {
      parts.push('没找到能盯的商品——先搜到商品，再说「帮我盯着价格」')
      return parts.join('\n')
    }
    if (cfg?.active === false) {
      parts.push(`这 ${ids.length} 件商品的盯价没注册上，待会儿再让我盯一次`)
      return parts.join('\n')
    }
    const th = cfg?.threshold
    const pct = cfg?.drop_percent
    const below = cfg?.below ?? 0
    if (th == null && pct == null) {
      // 后台只在「现价 <= 阈值」或「现价跌破基准价 X%」时才推提醒，
      // 两个口径都没说就是一条都不会推，得如实讲
      parts.push(`已盯上这 ${ids.length} 件商品，但你没说降到多少提醒你`)
      parts.push('补一句目标价（比如「降到 800 告诉我」或「降价 10% 提醒我」），我才能在有降价时叫你')
    } else if (th == null) {
      // 百分比口径：按盯价那刻的基准价折算，跌 X% 就叫
      parts.push(`已开始盯价：这 ${ids.length} 件商品，比现在降 ${pct}% 就提醒你`)
    } else if (below > 0) {
      // 目标价比现价还高的情况照注册：用户要的其实是「比现在便宜就叫我」。
      // 得说清是拿现价当基准，不然他以为只有掉到 ¥X 才会响，干等一场空
      parts.push(`已开始盯价：${ids.length} 件`)
      parts.push(
        `其中 ${below} 件现在就已经在 ¥${th} 以下了，我按它们现在的价盯着——比现在便宜就叫你`,
      )
    } else {
      parts.push(`已开始盯价：这 ${ids.length} 件商品，降到 ¥${th} 就提醒你`)
    }
    return parts.join('\n')
  }

  // 指代落空 / 没选出可下单的商品：这两种情况后端不会弹确认框（没商品可确认），
  // 气泡里必须说清算不上下单、缺的是什么，不然用户以为下单成功了
  if (o.ref_unresolved) {
    parts.push('你说的是「第一个」，但这个会话里我还没搜到过商品——先说要找什么，我搜出来你再挑一款下单')
    return parts.join('\n')
  }
  if (o.order_skipped === 'no_product') {
    parts.push('这句里说了下单，但我没选出能下单的商品——先搜到想要的、选定一款，我再跟你确认')
    return parts.join('\n')
  }

  // 搜空：诚实提示，不再拿假商品糊弄
  if (o.search_empty) {
    parts.push('没找到符合条件的商品——换个说法或放宽点预算再试试？')
    return parts.join('\n')
  }

  // 比价明细不在这儿铺开：几十件商品逐条列会把气泡撑满一整屏，
  // 明细交给下面的商品卡片和详情页的比价表，气泡里只报条数和最低价那件
  const table = (o.comparison_table ?? []).filter((r) => r.price != null)
  const count = new Set(table.map((r) => r.product_id)).size || (o.product_ids?.length ?? 0)
  if (count) parts.push(`帮你找到 ${count} 件商品，点开商品看各平台价格`)

  // 最低价推荐
  const sel = o.selected_product
  if (sel && sel.price != null) {
    parts.push(`推荐：${sel.name ?? sel.product_id}，${sel.platform} 最低 ¥${sel.price}`)
    // 风险只说推荐的那件：每件的差评率/店铺分卡片上都有，不用在气泡里再抄一遍
    const selRisk = sel.product_id ? o.risk_report?.[sel.product_id] : undefined
    if (selRisk?.risk_assessed) parts.push(`风险：${riskText(selRisk)}`)
  }

  if (o.order_confirmed === true) parts.push('已下单并归档')
  if (o.order_confirmed === false) parts.push('已取消下单')

  // 没凑出任何一句人话（比如没归到具体动作、也没搜到东西）就兜一句提示。
  // 以前这里会 return raw——那可是内部快照的 JSON，甩给用户等于给他看日志
  return parts.length ? parts.join('\n') : '这句我还没太听懂——你可以直接说想找什么商品，或者要盯哪件商品的价格。'
}

// 从结论快照里抽出这轮涉及的商品 id，拿去拉卡片数据渲染成可点的商品卡
function messageIds(raw: string): string[] {
  try {
    const o = JSON.parse(raw) as Snapshot
    const ids = new Set<string>()
    for (const r of o.comparison_table ?? []) if (r.product_id) ids.add(r.product_id)
    if (o.selected_product?.product_id) ids.add(o.selected_product.product_id)
    for (const id of o.product_ids ?? []) ids.add(id)
    return [...ids]
  } catch {
    return []
  }
}

async function consumeStream(
  resp: Response,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  if (!resp.body) return
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      const text = line.slice(5).trim()
      if (!text) continue
      onFrame(JSON.parse(text) as SseFrame)
    }
  }
}

export default function AgentTrace({
  sessionId,
  onHistoryChange,
  onOpenProduct,
  onOrderConfirmed,
}: Props) {
  const [message, setMessage] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [running, setRunning] = useState(false)
  const [phase, setPhase] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingApproval | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [notifies, setNotifies] = useState<PriceNotify[]>([])

  // 把历史里各轮涉及的商品 id 汇总，一次拉卡片数据
  const allIds = useMemo(() => {
    const s = new Set<string>()
    for (const m of messages) {
      if (m.role === 'assistant') for (const id of messageIds(m.content)) s.add(id)
    }
    return [...s]
  }, [messages])
  const productMap = useProducts(allIds)

  // 盯价降价提醒：每 30 秒轮询一次，读完即清空（服务端 drain），在本地累积展示
  useEffect(() => {
    setNotifies([])
    const poll = () => {
      fetch(`/api/sessions/${sessionId}/notify`, { headers: authHeaders() })
        .then((r) => r.json())
        .then((data: { notifications?: PriceNotify[] }) => {
          // 这里不判断「组件是否已卸载」就丢弃结果：接口是读完即清空，响应一到手
          // 服务端那份就删了，丢掉等于永久丢。开发模式（StrictMode）会把 effect 跑两遍，
          // 第一次请求正好会被那次多余的清理标记成已取消 → 一开会话提醒全没。
          if (data.notifications?.length)
            setNotifies((prev) => [...prev, ...data.notifications!])
        })
        .catch(() => undefined)
    }
    poll()
    const timer = setInterval(poll, 30_000)
    return () => clearInterval(timer)
  }, [sessionId])

  useEffect(() => {
    let cancelled = false
    setMessages([])
    setPhase(null)
    setPending(null)
    setNotice(null)
    fetch(`/api/sessions/${sessionId}/messages`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data: { messages: ChatMessage[] }) => {
        if (!cancelled) setMessages(data.messages ?? [])
      })
      .catch(() => undefined)
    // 刷新页面会丢掉审批弹窗，从后端把卡在审批点的会话找回来
    fetch(`/api/approval/${sessionId}`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data: { pending: PendingApproval | null }) => {
        if (!cancelled && data.pending) setPending(data.pending)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [sessionId])

  async function refreshHistory() {
    try {
      const data = await (
        await fetch(`/api/sessions/${sessionId}/messages`, { headers: authHeaders() })
      ).json()
      setMessages(data.messages ?? [])
      onHistoryChange()
    } catch {
      /* 拉历史失败不挡主流程 */
    }
  }

  function handleFrame(frame: SseFrame) {
    if (frame.__meta__) {
      if (frame.__meta__.note === 'expired') setNotice('审批已超时，系统自动取消了')
      return
    }
    if (frame.duplicate) {
      setNotice('这条消息刚刚发过，别着急再点一次')
      return
    }
    if (frame.__interrupt__?.length) {
      setPhase(null)
      setPending(frame.__interrupt__[0].value)
      return
    }
    // 节点帧只翻成一句「正在做什么」。节点名和原始 JSON 是内部实现，
    // 不给用户看——他要的是「AI 在干活」和最后的结果，不是我们的流程图
    for (const node of Object.keys(frame)) {
      if (node.startsWith('__')) continue
      setPhase(PHASE_TEXT[node] ?? '正在处理…')
    }
  }

  async function sendText(text: string) {
    const t = text.trim()
    if (!t || running) return
    setRunning(true)
    setNotice(null)
    setMessage('')
    setMessages((prev) => [...prev, { role: 'user', content: t }])
    try {
      const resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ message: t, session_id: sessionId }),
      })
      await consumeStream(resp, handleFrame)
      await refreshHistory()
    } catch {
      setNotice('连接断了，请重试')
    } finally {
      setRunning(false)
      setPhase(null)
    }
  }

  function send() {
    sendText(message)
  }

  async function decide(approved: boolean, reason?: string) {
    setRunning(true)
    setNotice(null)
    try {
      const resp = await fetch(`/api/approval/${sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ approved, reason }),
      })
      await consumeStream(resp, handleFrame)
      await refreshHistory()
      if (approved) onOrderConfirmed?.()  // 确认了就去「我的订单」看这一笔
    } catch {
      setNotice('审批提交失败，请重试')
    } finally {
      setRunning(false)
      setPending(null)
    }
  }

  return (
    <div>
      <div className="input-bar">
        <input
          className="input-field"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="比如：帮我找一款 2000 块以内的降噪耳机，然后下单"
          disabled={running}
        />
        <button className="btn btn-primary" onClick={send} disabled={running}>
          {running ? (
            <span className="thinking-dots">
              <span /> <span /> <span />
            </span>
          ) : '发送'}
        </button>
      </div>

      {notice && <div className="notice-bar">{notice}</div>}

      {/* 盯价降价提醒 */}
      {notifies.length > 0 && (
        <div className="notify-section">
          {notifies.map((n, i) => (
            <div key={i} className="notify-card">
              <span className="notify-badge">降价提醒</span>
              <span title={n.name || n.product_id}>
                {notifyName(n)} @ {n.platform} 已到 ¥{n.price}
                {n.drop_percent != null
                  ? `（比之前降了 ${n.drop_percent}% 起）`
                  : n.threshold != null
                    ? `（阈值 ¥${n.threshold}）`
                    : ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* 历史对话气泡 */}
      <div className="chat-list">
        {messages.map((m, i) => {
          const cards: Product[] =
            m.role === 'assistant'
              ? messageIds(m.content)
                  .map((id) => productMap[id])
                  .filter((p): p is Product => !!p)
              : []
          return (
            <div key={i} className={`bubble ${m.role === 'user' ? 'bubble-user' : 'bubble-assistant'}`}>
              <div className="bubble-text">
                {m.role === 'assistant' ? assistantText(m.content) : m.content}
              </div>
              {cards.length > 0 && (
                <div className="bubble-cards">
                  {cards.map((p) => (
                    <ProductCard
                      key={p.product_id}
                      product={p}
                      onClick={() => onOpenProduct?.(p.product_id)}
                    />
                  ))}
                </div>
              )}
            </div>
          )
        })}

        {/* 干活中的状态：就一句「正在做什么」，不带节点名、不带原始数据 */}
        {running && (
          <div className="bubble bubble-assistant bubble-running">
            <div className="bubble-text">
              <span className="thinking-dots">
                <span /> <span /> <span />
              </span>{' '}
              {phase ?? '正在处理…'}
            </div>
          </div>
        )}
      </div>

      {messages.length === 0 && !running && (
        <div className="empty-hint">
          还没有对话。发一句「找个耳机然后下单」可以体验审批流。
        </div>
      )}

      {pending && (
        <ApprovalModal pending={pending} submitting={running} onDecide={decide} />
      )}
    </div>
  )
}
