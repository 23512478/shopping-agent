import { FormEvent, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import ProductCard from '../components/ProductCard'
import { Product, fetchSearch } from '../catalog'

export default function Search() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const q = params.get('q') ?? ''
  const budget = params.get('budget') ?? ''

  const [input, setInput] = useState(q)
  const [loading, setLoading] = useState(true)
  const [products, setProducts] = useState<Product[]>([])
  const [empty, setEmpty] = useState(false)
  const [degraded, setDegraded] = useState(false)
  const [reload, setReload] = useState(0)  // 点「重试」自增，重新触发下面的请求

  useEffect(() => setInput(q), [q])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchSearch(q, budget ? Number(budget) : undefined).then((res) => {
      if (cancelled) return
      setProducts(res.products)
      setEmpty(res.empty)
      setDegraded(res.degraded)
      setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [q, budget, reload])

  function submit(e: FormEvent) {
    e.preventDefault()
    const keyword = input.trim()
    if (!keyword) return
    setParams({ q: keyword })
  }

  return (
    <div className="page search">
      <form className="search-bar" onSubmit={submit}>
        <input
          className="input-field"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="搜索商品，比如：降噪耳机 / 4K 显示器"
        />
        <button className="btn btn-primary" type="submit">
          搜索
        </button>
      </form>

      <div className="section-head">
        <h2>
          {q ? `“${q}” 的搜索结果` : '全部商品'}
          {!loading && !empty && <span className="result-count">共 {products.length} 件</span>}
        </h2>
        {budget && <span className="filter-chip">预算 ¥{budget} 以内</span>}
      </div>

      {loading && <div className="skeleton-grid">正在挑货…</div>}

      {!loading && empty && (
        <div className="empty-hint">
          {degraded
            ? '数据源这会儿响应慢或没接通，可能不是真没货，点重试再看一次。'
            : '没找到符合条件的商品，换个说法或放宽点预算再试试？'}
          <div className="retry-row">
            <button className="btn retry-btn" onClick={() => setReload((n) => n + 1)}>
              重试
            </button>
          </div>
        </div>
      )}

      {!loading && !empty && (
        <div className="card-grid">
          {products.map((p) => (
            <ProductCard
              key={p.product_id}
              product={p}
              onClick={() => navigate(`/product/${p.product_id}`)}
            />
          ))}
        </div>
      )}
    </div>
  )
}