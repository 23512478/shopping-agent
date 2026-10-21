import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authHeaders } from '../api'
import { monitorTitle } from './Me'

interface MonitorItem {
  id: number
  product_ids: string[]
  names?: string[]
  threshold?: number | null
  /** 按百分比盯的降幅（「降价 10% 提醒我」→ 10），和 threshold 二选一 */
  drop_percent?: number | null
  active?: boolean
  created_at?: string
}

interface Point {
  platform: string
  price: number
  at: string | null
}

interface Series {
  product_id: string
  name?: string | null
  points: Point[]
}

interface PriceNotify {
  type: string
  product_id?: string
  platform?: string
  price?: number
  threshold?: number
  /** 按百分比盯的降幅：有值就显示「降价 X% 触发」 */
  drop_percent?: number | null
  name?: string
}

// 商品名太长，卡片里只留前 20 个字，完整名字挂 title
function shortName(n: PriceNotify): string {
  const name = n.name || n.product_id || '商品'
  return name.length > 20 ? `${name.slice(0, 20)}…` : name
}

/** 价格走势：一个平台一条折线，横轴按记录顺序等距（时间间隔本来就不均匀，按索引更稳）。
 *  价格一直没动就是一条水平线——如实画，不编趋势。 */
function PriceChart({ points }: { points: Point[] }) {
  if (!points.length) return <div className="monitor-empty">还没有价格记录，等下一轮查价</div>

  const entries = points.map((p, i) => ({ ...p, i }))
  const byPlat = new Map<string, typeof entries>()
  for (const e of entries) {
    const arr = byPlat.get(e.platform) ?? []
    arr.push(e)
    byPlat.set(e.platform, arr)
  }

  const values = entries.map((e) => e.price)
  const max = Math.max(...values)
  const min = Math.min(...values)
  const span = max - min || 1 // 价格没变过，给个假跨度画成水平线就行
  const W = 320
  const H = 120
  const PAD = 12
  const x = (i: number) =>
    entries.length === 1 ? W / 2 : PAD + (i * (W - PAD * 2)) / (entries.length - 1)
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2)

  return (
    <div className="price-chart">
      <svg viewBox={`0 0 ${W} ${H}`} className="price-chart-svg" role="img" aria-label="价格走势">
        <line x1={PAD} y1={y(min)} x2={W - PAD} y2={y(min)} className="chart-axis" />
        {[...byPlat.entries()].map(([plat, list], k) => (
          <g key={plat}>
            {list.length > 1 && (
              <polyline
                className={`chart-line chart-c${k % 3}`}
                points={list.map((e) => `${x(e.i)},${y(e.price)}`).join(' ')}
              />
            )}
            {list.map((e) => (
              <circle
                key={e.i}
                cx={x(e.i)}
                cy={y(e.price)}
                r={3}
                className={`chart-dot chart-c${k % 3}`}
              />
            ))}
          </g>
        ))}
      </svg>
      <div className="chart-legend">
        {[...byPlat.entries()].map(([plat, list], k) => (
          <span key={plat} className="chart-legend-item">
            <i className={`chart-swatch chart-c${k % 3}`} />
            {plat} 最低 ¥{Math.min(...list.map((e) => e.price))}
          </span>
        ))}
        <span className="chart-legend-item chart-legend-range">
          区间 ¥{min} – ¥{max}
        </span>
      </div>
    </div>
  )
}

export default function Monitor() {
  const navigate = useNavigate()
  const [monitors, setMonitors] = useState<MonitorItem[]>([])
  const [loading, setLoading] = useState(true)
  const [notifies, setNotifies] = useState<PriceNotify[]>([])
  const [openId, setOpenId] = useState<number | null>(null)
  const [history, setHistory] = useState<Record<number, Series[]>>({})

  const refresh = useCallback(async () => {
    try {
      const data = await (await fetch('/api/monitor', { headers: authHeaders() })).json()
      setMonitors(data.monitors ?? [])
    } catch {
      /* Redis/PG 没起时列表空着，不挡用 */
    }
  }, [])

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [refresh])

  // 降价提醒：30 秒轮询一次。跟聊天页一样不判「已卸载」就丢结果——
  // 接口读完即清空，丢了就永久没了（开发模式 StrictMode 会跑两遍 effect）
  useEffect(() => {
    const poll = () => {
      fetch('/api/monitor/notify', { headers: authHeaders() })
        .then((r) => r.json())
        .then((d: { notifications?: PriceNotify[] }) => {
          if (d.notifications?.length) setNotifies((prev) => [...prev, ...d.notifications!])
        })
        .catch(() => undefined)
    }
    poll()
    const timer = setInterval(poll, 30_000)
    return () => clearInterval(timer)
  }, [])

  async function toggle(id: number) {
    if (openId === id) {
      setOpenId(null)
      return
    }
    setOpenId(id)
    if (history[id]) return
    try {
      const data = await (await fetch(`/api/monitor/${id}/history`, { headers: authHeaders() })).json()
      setHistory((prev) => ({ ...prev, [id]: data.series ?? [] }))
    } catch {
      setHistory((prev) => ({ ...prev, [id]: [] }))
    }
  }

  async function cancel(id: number) {
    try {
      const r = await fetch(`/api/monitor/${id}`, { method: 'DELETE', headers: authHeaders() })
      if (!r.ok) return
      setMonitors((prev) => prev.filter((m) => m.id !== id))
      setOpenId((cur) => (cur === id ? null : cur))
    } catch {
      /* 网络抖动保持原样，用户可再点一次 */
    }
  }

  if (loading) return <div className="page monitor"><div className="skeleton-grid">加载中…</div></div>

  return (
    <div className="page monitor">
      <section className="monitor-hero">
        <h1>盯价</h1>
        <p>盯着的商品、目标价、价格走势都在这儿</p>
      </section>

      {notifies.length > 0 && (
        <section className="section">
          <div className="section-head">
            <h2>降价提醒</h2>
          </div>
          <div className="notify-section">
            {notifies.map((n, i) => (
              <div key={i} className="notify-card">
                <span className="notify-badge">降价提醒</span>
                <span title={n.name || n.product_id}>
                  {shortName(n)} @ {n.platform} 已到 ¥{n.price}
                  {n.drop_percent != null
                    ? `（比之前降了 ${n.drop_percent}% 起）`
                    : n.threshold != null
                      ? `（阈值 ¥${n.threshold}）`
                      : ''}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="section">
        <div className="section-head">
          <h2>盯价中的商品</h2>
          <button className="btn head-more" onClick={() => navigate('/search')}>
            去挑商品盯 →
          </button>
        </div>

        {monitors.length === 0 ? (
          <div className="me-empty">
            还没有在盯的商品。搜到心仪的，点商品卡片上的「盯价」就行。
          </div>
        ) : (
          <div className="monitor-list">
            {monitors.map((m) => (
              <div key={m.id} className="monitor-card">
                <div className="monitor-top">
                  <div className="monitor-name" title={(m.names ?? m.product_ids).join('、')}>
                    {monitorTitle(m)}
                  </div>
                  <div className="monitor-btns">
                    <button className="btn" onClick={() => toggle(m.id)}>
                      {openId === m.id ? '收起价格' : '价格走势'}
                    </button>
                    <button
                      className="btn monitor-cancel"
                      onClick={() => cancel(m.id)}
                      aria-label={`取消盯价 ${monitorTitle(m)}`}
                    >
                      取消
                    </button>
                  </div>
                </div>
                <div className="monitor-meta">
                  {m.threshold != null ? (
                    <span>目标价 ¥{m.threshold}</span>
                  ) : m.drop_percent != null ? (
                    <span>降价 {m.drop_percent}% 提醒</span>
                  ) : (
                    <span>只记价格，没设目标价</span>
                  )}
                  <span className={m.active ? 'monitor-on' : 'monitor-off'}>
                    {m.active ? '监控中' : '已暂停'}
                  </span>
                </div>

                {openId === m.id && (
                  <div className="monitor-history">
                    {(history[m.id] ?? []).map((s) => {
                      const last = s.points[s.points.length - 1]
                      const reached = last && m.threshold != null && last.price <= m.threshold
                      return (
                        <div key={s.product_id} className="monitor-series">
                          <div className="monitor-series-head">
                            <span className="monitor-series-name" title={s.name ?? s.product_id}>
                              {s.name ?? s.product_id}
                            </span>
                            {last && (
                              <span className={reached ? 'monitor-hit' : 'monitor-last'}>
                                最近 ¥{last.price}
                                {reached ? '（已达目标价）' : ''}
                              </span>
                            )}
                          </div>
                          <PriceChart points={s.points} />
                        </div>
                      )
                    })}
                    {(history[m.id] ?? []).length === 0 && (
                      <div className="monitor-empty">这个商品还没查到价格记录</div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
