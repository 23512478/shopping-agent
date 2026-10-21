import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import ProductCard from '../components/ProductCard'
import { readRecent, useProducts } from '../catalog'

const CATEGORIES = [
  { key: '耳机', hint: '降噪 / 真无线' },
  { key: '手机', hint: '旗舰 / 影像' },
  { key: '键盘', hint: '办公 / 客制化' },
  { key: '显示器', hint: '4K / 高刷' },
]

export default function Home() {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [recent] = useState<string[]>(readRecent)
  const recentMap = useProducts(recent)

  function submit(e: FormEvent) {
    e.preventDefault()
    const keyword = q.trim()
    if (keyword) navigate(`/search?q=${encodeURIComponent(keyword)}`)
  }

  return (
    <div className="page home">
      <section className="hero">
        <h1 className="hero-title">
          想买什么，<span className="grad-text">说一句就行</span>
        </h1>
        <p className="hero-sub">AI 帮你跨平台搜货、比价、排查差评，选中直接去平台下单</p>

        <form className="hero-search" onSubmit={submit}>
          <input
            className="input-field"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="比如：2000 以内的降噪耳机"
          />
          <button className="btn btn-primary" type="submit">
            搜索
          </button>
        </form>

        <div className="hero-cats">
          {CATEGORIES.map((c) => (
            <button
              key={c.key}
              className="cat-chip"
              onClick={() => navigate(`/search?q=${encodeURIComponent(c.key)}`)}
            >
              <strong>{c.key}</strong>
              <span>{c.hint}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="entry-card" onClick={() => navigate('/chat')} role="button" tabIndex={0}>
        <div className="entry-icon">AI</div>
        <div className="entry-body">
          <h3>AI 导购</h3>
          <p>用人话描述需求，Agent 自动搜货、比价、看差评，一路帮你到下单</p>
        </div>
        <span className="entry-go">去对话 →</span>
      </section>

      {recent.length > 0 && (
        <section className="section">
          <div className="section-head">
            <h2>最近浏览</h2>
          </div>
          <div className="card-grid">
            {recent
              .map((id) => recentMap[id])
              .filter(Boolean)
              .map((p) => (
                <ProductCard
                  key={p.product_id}
                  product={p}
                  onClick={() => navigate(`/product/${p.product_id}`)}
                />
              ))}
          </div>
        </section>
      )}
    </div>
  )
}