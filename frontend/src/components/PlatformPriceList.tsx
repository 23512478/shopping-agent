import { PlatformCell } from '../catalog'

interface Props {
  platforms: Record<string, PlatformCell>
}

// 多平台比价表：按价格升序，最低价高亮，每行一个跳转第三方平台的按钮。
// 按钮文案跟落点一致：精准商品页说「去 XX 购买」，搜索页兜底就说「去 XX 搜索」，不骗用户。
export default function PlatformPriceList({ platforms }: Props) {
  const entries = Object.entries(platforms ?? {}).sort((a, b) => a[1].price - b[1].price)
  if (!entries.length) return <div className="pp-empty">暂无平台报价</div>
  const best = entries[0][0]

  return (
    <div className="pplist">
      {entries.map(([plat, cell]) => (
        <div key={plat} className={`pprow ${plat === best ? 'cheapest' : ''}`}>
          <span className="ppplat">
            {plat}
            {plat === best && <span className="pp-best">全网最低</span>}
          </span>
          <span className="ppprice">
            <em>¥</em>
            {cell.price}
          </span>
          <a
            className="btn btn-primary ppbuy"
            href={cell.url || '#'}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => !cell.url && e.preventDefault()}
          >
            去{plat}
            {cell.url_kind === 'search' ? '搜索' : '购买'}
          </a>
        </div>
      ))}
    </div>
  )
}