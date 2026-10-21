import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authHeaders } from '../api'

interface Order {
  id: number
  product_id: string
  name?: string
  platform: string
  price?: number | null
  image?: string
  url?: string
  url_kind?: 'product' | 'search'
  status: string
  created_at?: string
}

export default function Orders() {
  const navigate = useNavigate()
  const [orders, setOrders] = useState<Order[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    fetch('/api/orders', { headers: authHeaders() })
      .then((r) => r.json())
      .then((d: { orders?: Order[] }) => {
        if (!cancelled) setOrders(d.orders ?? [])
      })
      .catch(() => undefined)
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="page orders">
      <div className="section-head">
        <h2>我的订单</h2>
        <span className="section-sub">站内不收款，点「去平台购买」跳到官方下单</span>
      </div>

      {loading && <div className="skeleton-grid">加载中…</div>}

      {!loading && orders.length === 0 && (
        <div className="empty-hint">
          还没有订单。
          <br />
          去 <span className="link-text" onClick={() => navigate('/chat')}>AI 导购</span> 说一句「帮我买个耳机」试试。
        </div>
      )}

      <div className="order-list">
        {orders.map((o) => (
          <div key={o.id} className="order-card">
            <div className="pcard-img order-img" onClick={() => navigate(`/product/${o.product_id}`)}>
              {o.image ? (
                <img src={o.image} alt={o.name ?? o.product_id} loading="lazy" />
              ) : (
                <span className="pcard-img-fallback">单</span>
              )}
            </div>

            <div className="order-body">
              <div className="order-name" onClick={() => navigate(`/product/${o.product_id}`)}>
                {o.name ?? o.product_id}
              </div>
              <div className="order-meta">
                <span className="pcard-platform">{o.platform}</span>
                <span className="order-status">{o.status === 'intent' ? '待购买' : o.status}</span>
                {o.created_at && <span className="order-time">{new Date(o.created_at).toLocaleString()}</span>}
              </div>
            </div>

            <div className="order-right">
              <span className="pcard-price">
                <em>¥</em>
                {o.price ?? '--'}
              </span>
              <a
                className="btn btn-primary"
                href={o.url || '#'}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => !o.url && e.preventDefault()}
              >
                去{o.platform}
                {o.url_kind === 'search' ? '搜索' : '购买'}
              </a>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}