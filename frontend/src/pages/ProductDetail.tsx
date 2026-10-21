import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import MonitorButton from '../components/MonitorButton'
import PlatformPriceList from '../components/PlatformPriceList'
import { authHeaders } from '../api'
import { Product, fetchProduct, minPrice, pushRecent } from '../catalog'

interface Advice {
  verdict: string
  tone: 'good' | 'mixed' | 'careful'
  points: string[]
}

// 参谋规则版：大模型没配/调不通时的兜底，保证用户点一下总有一份结论落地。
// 摆三个数：各平台价格（差价多少）、差评率（口碑硬不硬）、店铺口碑（店靠不靠谱），
// 全是这件商品自己的真数据推出来的。正常路径走 /api/advice，让大模型基于同一批数据分析
function adviceFor(p: Product): Advice {
  const rows = Object.entries(p.platforms ?? {})
    .filter(([, c]) => c?.price != null)
    .map(([platform, c]) => ({ platform, price: c.price, searchOnly: c.url_kind === 'search' }))
    .sort((a, b) => a.price - b.price)

  const points: string[] = []
  const best = rows[0]
  if (!best) {
    return { verdict: '这件商品一个平台的价格都没拿到，先别急', tone: 'careful', points: ['价格数据是空的，等数据源恢复了再看'] }
  }

  // 一、价格：摆出来、说清最低在哪、差价多大
  points.push(
    `各平台：${rows.map((r) => `${r.platform} ¥${r.price}`).join(' / ')}，最低在${best.platform}（¥${best.price}）`,
  )
  const top = rows[rows.length - 1]
  if (rows.length > 1) {
    const diff = top.price - best.price
    const pct = (diff / best.price) * 100
    points.push(
      pct < 5
        ? `跟${top.platform}（¥${top.price}）只差 ¥${diff.toFixed(1)}，差价不大，挑你信得过的平台就行`
        : `比${top.platform}（¥${top.price}）便宜 ¥${diff.toFixed(1)}（约 ${pct.toFixed(0)}%），想省钱就在${best.platform}下单`,
    )
  }
  // 有平台没有直达商品页就得说清：点过去只是搜索页，别让人以为点进去就能买
  const searchOnly = rows.filter((r) => r.searchOnly).map((r) => r.platform)
  if (searchOnly.length) {
    points.push(`${searchOnly.join('、')}没有直达这件商品的页面，点过去是搜索结果页，要自己再找一下`)
  }

  // 二、口碑：差评率
  const nr = p.negative_rate
  if (nr == null) {
    points.push('差评率没拿到数据，这块没法评')
  } else if (nr <= 0.02) {
    points.push(`差评率 ${(nr * 100).toFixed(1)}%，算低的——口碑没什么可担心的`)
  } else if (nr <= 0.05) {
    points.push(`差评率 ${(nr * 100).toFixed(1)}%，不高不低，下单前翻两条差评看说的是什么`)
  } else {
    points.push(`差评率 ${(nr * 100).toFixed(1)}%，偏高——差评集中的问题（做工、售后之类）先确认能不能接受`)
  }

  // 三、店铺
  const ss = p.shop_score
  if (ss != null) {
    const label = p.shop_score_source === 'shop_level' ? '店铺等级' : '店铺评分'
    if (ss >= 4.5) points.push(`${label} ${ss.toFixed(1)} 分，店铺这边靠谱`)
    else if (ss >= 4.0) points.push(`${label} ${ss.toFixed(1)} 分，中等水平`)
    else points.push(`${label} ${ss.toFixed(1)} 分，偏低，真出问题售后容易扯皮`)
  }

  const badRep = (nr != null && nr > 0.05) || (ss != null && ss < 4.0)
  if (badRep) {
    return {
      verdict: `别急着买：最低价在${best.platform}（¥${best.price}），但口碑有硬伤，同类再看两家`,
      tone: 'careful',
      points,
    }
  }
  const fineRep = (nr == null || nr <= 0.03) && (ss == null || ss >= 4.5)
  if (fineRep) {
    return {
      verdict: `值得入手：${best.platform} ¥${best.price} 是全网最低，口碑也没硬伤`,
      tone: 'good',
      points,
    }
  }
  return {
    verdict: `${best.platform} ¥${best.price} 价格可以，口碑一般——图便宜能入，求省心再多比两家`,
    tone: 'mixed',
    points,
  }
}

export default function ProductDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [product, setProduct] = useState<Product | null>(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)  // 点「重试」自增，重新拉一次详情
  const [advice, setAdvice] = useState<Advice | null>(null)
  const [adviceLoading, setAdviceLoading] = useState(false)
  const [adviceSource, setAdviceSource] = useState<'llm' | 'rules' | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setLoading(true)
    setAdvice(null)          // 换商品了，上一件的参谋结论别带过来
    setAdviceSource(null)
    fetchProduct(id).then((p) => {
      if (cancelled) return
      setProduct(p)
      setLoading(false)
      if (p) pushRecent(p.product_id)
    })
    return () => {
      cancelled = true
    }
  }, [id, reload])

  // 参谋：先让后端把这件商品的真实数据喂给大模型分析；模型没配/调不通就落回本地规则版。
  // 两条路都基于同一批真数据，用户点一下必有结论，也不用担心模型自己编价格。
  async function askAdvice() {
    if (!product) return
    if (advice) {
      setAdvice(null)
      setAdviceSource(null)
      return
    }
    setAdviceLoading(true)
    try {
      const r = await fetch('/api/advice', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ product_id: product.product_id }),
      })
      const data = await r.json()
      if (data?.ok && data.verdict) {
        setAdvice({ verdict: data.verdict, tone: data.tone ?? 'mixed', points: data.points ?? [] })
        setAdviceSource('llm')
        return
      }
    } catch {
      // 网络或后端出问题：往下走，用本地规则兜住，别把报错甩给用户
    } finally {
      setAdviceLoading(false)
    }
    setAdvice(adviceFor(product))
    setAdviceSource('rules')
  }

  if (loading) return <div className="page detail"><div className="skeleton-grid">加载中…</div></div>

  if (!product) {
    return (
      <div className="page detail">
        <button className="btn back-btn" onClick={() => navigate(-1)}>
          ← 返回
        </button>
        <div className="empty-hint">
          这件商品暂时查不到了（也可能是数据源响应慢），点重试再看一次？
          <div className="retry-row">
            <button className="btn retry-btn" onClick={() => setReload((n) => n + 1)}>
              重试
            </button>
          </div>
        </div>
      </div>
    )
  }

  const price = minPrice(product)

  return (
    <div className="page detail">
      <button className="btn back-btn" onClick={() => navigate(-1)}>
        ← 返回
      </button>

      <div className="detail-main">
        <div className="detail-img">
          {product.image ? (
            <img src={product.image} alt={product.name} />
          ) : (
            <span className="pcard-img-fallback">{(product.brand || '商').slice(0, 1)}</span>
          )}
        </div>

        <div className="detail-info">
          <h1 className="detail-name">{product.name}</h1>
          <div className="pcard-tags">
            {(product.tags ?? []).map((t) => (
              <span key={t} className="pcard-tag">
                {t}
              </span>
            ))}
          </div>

          <div className="detail-price">
            <span className="detail-price-label">全网最低</span>
            <span className="detail-price-num">
              <em>¥</em>
              {price ?? '--'}
            </span>
          </div>

          <div className="detail-stats">
            {product.shop_score != null && (
              <span className="stat">
                <b>{product.shop_score.toFixed(1)}</b>{' '}
                {product.shop_score_source === 'shop_level' ? '店铺等级' : '店铺评分'}
              </span>
            )}
            {product.negative_rate != null && (
              <span className="stat">
                <b>{(product.negative_rate * 100).toFixed(1)}%</b> 差评率
              </span>
            )}
            {product.category && <span className="stat">{product.category}</span>}
          </div>

          {product.description && <p className="detail-desc">{product.description}</p>}

          {/* 操作区：参谋 + 盯价，跟搜索卡片上的入口对齐，详情页不该少 */}
          <div className="detail-actions">
            <button
              className="btn btn-primary detail-ai"
              onClick={askAdvice}
              disabled={adviceLoading}
            >
              {adviceLoading ? '正在分析…' : advice ? '收起参谋建议' : '帮我参谋一下'}
            </button>
            <MonitorButton product={product} />
          </div>
        </div>
      </div>

      {/* 参谋结论就落在这件商品的卡片下面，原地看，不跳对话页、也不去别处搜一遍 */}
      {advice && (
        <section className="advice-panel" data-testid="advice">
          <div className="advice-head">
            <span className={`advice-verdict advice-${advice.tone}`}>{advice.verdict}</span>
            <span className="advice-note">
              {adviceSource === 'llm'
                ? 'AI 分析，依据这件商品的各平台价格、差评率、店铺口碑'
                : '本地规则兜底（AI 分析暂时不可用），依据同上'}
            </span>
          </div>
          <ul className="advice-points">
            {advice.points.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="section">
        <div className="section-head">
          <h2>多平台比价</h2>
          <span className="section-sub">点「购买」跳到对应平台，站内不收款</span>
        </div>
        <PlatformPriceList platforms={product.platforms ?? {}} />
      </section>
    </div>
  )
}