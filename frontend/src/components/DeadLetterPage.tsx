import { useEffect, useState } from 'react'
import { authHeaders } from '../api'

interface DeadLetter {
  id: number
  node_id: string
  input: unknown
  last_error: string
  retry_history: unknown[]
  suggested_fix: string
  created_at: string
}

export default function DeadLetterPage() {
  const [items, setItems] = useState<DeadLetter[]>([])

  useEffect(() => {
    fetch('/api/deadletter', { headers: authHeaders() })
      .then((r) => r.json())
      .then((d) => setItems(d.items ?? []))
      .catch(() => setItems([]))
  }, [])

  if (items.length === 0) {
    return <div className="dl-empty">暂无失败任务</div>
  }

  return (
    <div className="dl-grid">
      {items.map((it) => (
        <div key={it.id} className="dl-card">
          <div className="dl-card-top">
            <span className="dl-node-badge">{it.node_id}</span>
            <span className="dl-time">{it.created_at}</span>
          </div>
          <div className="dl-field">
            <span className="dl-field-label">最后错误：</span>
            <span className="dl-field-value">{it.last_error}</span>
          </div>
          <div className="dl-field">
            <span className="dl-field-label">建议修复：</span>
            <span className="dl-field-value">{it.suggested_fix}</span>
          </div>
          <div className="dl-field">
            <span className="dl-field-label">输入：</span>
            <span className="dl-field-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>
              {JSON.stringify(it.input)}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}
