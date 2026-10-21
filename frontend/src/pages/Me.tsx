import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authHeaders } from '../api'

interface Summary {
  /** 这段对话的会话 id，点了能回 /chat?session=xxx 接着聊 */
  session_id?: string
  topics?: string[]
  preferences?: string[]
  brands?: string[]
  summary?: string
}

interface Monitor {
  id: number
  product_ids: string[]
  names?: string[]
  threshold?: number | null
  /** 按百分比盯的降幅，和 threshold 二选一 */
  drop_percent?: number | null
  active?: boolean
  created_at?: string
}

interface Profile {
  user_id: string
  preferences: string[]
  recent_summaries: Summary[]
  order_count: number
  monitors: Monitor[]
}

/** 盯价卡片标题：只列前两个商品名，其余折成「等 N 件」。
 *  一轮搜货能盯 30 件，把名字全铺出来就是一屏密密麻麻的字，看着像出错了。 */
export function monitorTitle(m: Monitor): string {
  const names = (m.names ?? m.product_ids).filter(Boolean)
  if (!names.length) return '未知商品'
  const head = names.slice(0, 2).join('、')
  return names.length > 2 ? `${head} 等 ${names.length} 件` : head
}

export default function Me() {
  const navigate = useNavigate()
  const [profile, setProfile] = useState<Profile | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    fetch('/api/profile', { headers: authHeaders() })
      .then((r) => r.json())
      .then((d: Profile) => {
        if (!cancelled) setProfile(d)
      })
      .catch(() => undefined)
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [])

  // 取消盯价：后端会连带摘掉调度任务，成功后本地把这条从列表里去掉
  const cancelMonitor = async (id: number) => {
    try {
      const r = await fetch(`/api/monitor/${id}`, { method: 'DELETE', headers: authHeaders() })
      if (!r.ok) return
      setProfile((prev) =>
        prev ? { ...prev, monitors: prev.monitors.filter((m) => m.id !== id) } : prev,
      )
    } catch {
      // 网络抖动就保持原样，用户可再点一次
    }
  }

  if (loading) return <div className="page me"><div className="skeleton-grid">加载中…</div></div>

  const p = profile
  const prefs = p?.preferences ?? []

  return (
    <div className="page me">
      <section className="me-hero">
        <div className="me-avatar">我</div>
        <div>
          <h1 className="me-name">我的主页</h1>
          <p className="me-sub">画像、订单、盯价都在这儿</p>
        </div>
      </section>

      <div className="me-stats">
        <div className="me-stat" onClick={() => navigate('/orders')} role="button" tabIndex={0}>
          <b>{p?.order_count ?? 0}</b>
          <span>我的订单</span>
        </div>
        <div className="me-stat">
          <b>{p?.monitors?.length ?? 0}</b>
          <span>盯价中</span>
        </div>
        <div className="me-stat">
          <b>{p?.recent_summaries?.length ?? 0}</b>
          <span>历史对话</span>
        </div>
      </div>

      <section className="section">
        <div className="section-head">
          <h2>AI 眼里的你</h2>
          <span className="section-sub">从历次对话里沉淀的稳定偏好</span>
        </div>
        {prefs.length ? (
          <div className="pref-wrap">
            {prefs.map((t, i) => (
              <span key={i} className="pref-chip">
                {t}
              </span>
            ))}
          </div>
        ) : (
          <div className="me-empty">还没有沉淀出偏好，多聊几次就有了</div>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2>我的盯价</h2>
          <button className="btn head-more" onClick={() => navigate('/monitor')}>
            盯价页 · 看价格走势 →
          </button>
        </div>
        {(p?.monitors?.length ?? 0) === 0 ? (
          <div className="me-empty">没有在盯的商品。搜到心仪的，点商品卡片上的「盯价」就行</div>
        ) : (
          <div className="monitor-list">
            {p!.monitors.map((m) => (
              <div key={m.id} className="monitor-card">
                <div className="monitor-top">
                  <div className="monitor-name" title={(m.names ?? m.product_ids).join('、')}>
                    {monitorTitle(m)}
                  </div>
                  <button
                    className="btn monitor-cancel"
                    onClick={() => cancelMonitor(m.id)}
                    aria-label={`取消盯价 ${monitorTitle(m)}`}
                  >
                    取消
                  </button>
                </div>
                <div className="monitor-meta">
                  {m.threshold != null && <span>目标价 ¥{m.threshold}</span>}
                  {m.threshold == null && m.drop_percent != null && (
                    <span>降价 {m.drop_percent}% 提醒</span>
                  )}
                  <span className={m.active ? 'monitor-on' : 'monitor-off'}>
                    {m.active ? '监控中' : '已暂停'}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2>历史对话</h2>
          <span className="section-sub">点一条接着聊</span>
        </div>
        {(p?.recent_summaries?.length ?? 0) === 0 ? (
          <div className="me-empty">还没有历史对话</div>
        ) : (
          <div className="summary-list">
            {p!.recent_summaries.map((s, i) => {
              // 摘要带 session_id 才是能点回去的；老数据没有就老实当普通卡片展示
              const sid = s.session_id
              const body = (
                <>
                  <div className="summary-text">{s.summary ?? '（无摘要）'}</div>
                  {!!s.topics?.length && (
                    <div className="pcard-tags">
                      {s.topics.map((t) => (
                        <span key={t} className="pcard-tag">
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                </>
              )
              return sid ? (
                <button
                  key={i}
                  className="summary-card summary-card-clickable"
                  onClick={() => navigate(`/chat?session=${encodeURIComponent(sid)}`)}
                  title="继续这段对话"
                >
                  {body}
                  <span className="summary-go">继续聊 →</span>
                </button>
              ) : (
                <div key={i} className="summary-card">
                  {body}
                </div>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}