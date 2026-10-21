import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import AgentTrace from '../components/AgentTrace'
import { authHeaders } from '../api'

interface SessionMeta {
  session_id: string
  title: string
  updated_at?: string
}

function newSessionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export default function Chat() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [sessions, setSessions] = useState<SessionMeta[]>([])
  // 带 ?session=xxx 进来就是「接着上次聊」——个人主页的历史对话点进来走这条
  const [activeSid, setActiveSid] = useState(() => params.get('session') || newSessionId())

  const refreshSessions = useCallback(async () => {
    try {
      const data = await (
        await fetch('/api/sessions', { headers: authHeaders() })
      ).json()
      setSessions(data.sessions ?? [])
    } catch {
      /* Redis 没起时列表就是空，不挡使用 */
    }
  }, [])

  useEffect(() => {
    refreshSessions()
  }, [refreshSessions])

  // 地址栏换会话（比如在「我的」里点了另一条历史）要跟着切
  useEffect(() => {
    const sid = params.get('session')
    if (sid && sid !== activeSid) setActiveSid(sid)
  }, [params, activeSid])

  function openSession(sid: string) {
    setActiveSid(sid)
    setParams({ session: sid })
  }

  function startNew() {
    openSession(newSessionId())
  }

  return (
    <div className="app-layout">
      {/* 左侧会话栏 */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <h2 className="sidebar-title">AI 导购</h2>
          <p className="sidebar-subtitle">搜货 · 比价 · 下单，一句话搞定</p>
        </div>

        <button className="btn btn-primary btn-new-chat" onClick={startNew}>
          + 新对话
        </button>

        <div className="session-list">
          {sessions.length === 0 && (
            <div className="session-empty">
              暂无历史会话
              <br />
              （起了 Redis 后会保留 7 天）
            </div>
          )}
          {sessions.map((s) => (
            <button
              key={s.session_id}
              className={`session-item ${s.session_id === activeSid ? 'active' : ''}`}
              onClick={() => openSession(s.session_id)}
              title={s.title}
            >
              {s.title}
            </button>
          ))}
        </div>
      </aside>

      {/* 右侧对话区 */}
      <main className="main-area">
        <AgentTrace
          key={activeSid}
          sessionId={activeSid}
          onHistoryChange={refreshSessions}
          onOpenProduct={(id) => navigate(`/product/${id}`)}
          onOrderConfirmed={() => navigate('/orders')}
        />
      </main>
    </div>
  )
}