import MonitorButton from './MonitorButton'
import { PlatformCell, Product, minPrice } from '../catalog'

interface Props {
  product: Product
  onClick?: () => void
}

// 商品卡片：图 + 名称 + 标签 + 平台标签 + 最低价 + 店铺分/差评率 + 盯价入口。
// 点击行为交给父级（搜索页/对话/首页各自决定跳哪）。
export default function ProductCard({ product, onClick }: Props) {
  const price = minPrice(product)
  const platforms = Object.keys(product.platforms ?? {})

  return (
    <div
      className="pcard"
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        // 焦点在卡片里的盯价按钮上时，回车/空格是按钮的事，别顺带把卡片也点开
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') onClick?.()
      }}
    >
      <div className="pcard-img">
        {product.image ? (
          <img src={product.image} alt={product.name} loading="lazy" />
        ) : (
          <span className="pcard-img-fallback">{(product.brand || product.name || '商').slice(0, 1)}</span>
        )}
      </div>

      <div className="pcard-body">
        <div className="pcard-name">{product.name}</div>
        <div className="pcard-tags">
          {(product.tags ?? []).slice(0, 3).map((t) => (
            <span key={t} className="pcard-tag">
              {t}
            </span>
          ))}
        </div>

        <div className="pcard-foot">
          <span className="pcard-price">
            <em>¥</em>
            {price ?? '--'}
          </span>
          <span className="pcard-platforms">
            {platforms.map((plat) => (
              <span key={plat} className="pcard-platform">
                {plat}
              </span>
            ))}
          </span>
        </div>

        <div className="pcard-meta">
          {product.shop_score != null && (
            <span>
              {product.shop_score_source === 'shop_level' ? '等级' : '店铺'}{' '}
              {product.shop_score.toFixed(1)} 分
            </span>
          )}
          {product.negative_rate != null && (
            <span>差评 {(product.negative_rate * 100).toFixed(1)}%</span>
          )}
        </div>

        {/* 盯价入口：看到心仪的直接盯上，不用再去对话里说一句 */}
        <div className="pcard-actions">
          <MonitorButton product={product} />
        </div>
      </div>
    </div>
  )
}

export function cheapestPlatform(platforms: Record<string, PlatformCell>): string | null {
  const entries = Object.entries(platforms ?? {})
  if (!entries.length) return null
  return entries.reduce((best, cur) => (cur[1].price < best[1].price ? cur : best))[0]
}