// 商品数据层：类型 + 读接口 + 卡片用的 hook。
// 页面只连后端 /api，不直连 MCP；后端挂了就返回空数组，页面自己兜空态。
import { useEffect, useState } from 'react'
import { authHeaders } from './api'

export interface PlatformCell {
  price: number
  url: string
  // product=精准商品落地页；search=平台搜索页兜底。缺省当 product，按钮文案照旧「购买」
  url_kind?: 'product' | 'search'
}

export interface Product {
  product_id: string
  name: string
  category?: string
  brand?: string
  tags?: string[]
  description?: string
  image?: string
  negative_rate?: number | null
  shop_score?: number | null
  // 口径来源：user_evaluate=京东店铺评分（10 分制折 5）；shop_level=拿不到评分时退的店铺等级。
  // 两种不是一个指标，缺省按「店铺评分」展示（mock 商品没这个字段）
  shop_score_source?: 'user_evaluate' | 'shop_level' | null
  platforms?: Record<string, PlatformCell>
}

export function minPrice(p: Product): number | null {
  const prices = Object.values(p.platforms ?? {}).map((c) => c.price)
  return prices.length ? Math.min(...prices) : null
}

// 已拉到详情的商品缓存进内存，列表 -> 详情页来回切不用重复请求
const cache = new Map<string, Product>()

export async function fetchProducts(ids: string[]): Promise<Product[]> {
  const missing = ids.filter((id) => id && !cache.has(id))
  if (missing.length) {
    try {
      const r = await fetch(`/api/products?ids=${encodeURIComponent(missing.join(','))}`, {
        headers: authHeaders(),
      })
      if (r.ok) {
        const data: { products?: Product[] } = await r.json()
        for (const p of data.products ?? []) cache.set(p.product_id, p)
      }
    } catch {
      /* 后端/MCP 没起就返回空，页面兜空态 */
    }
  }
  return ids.map((id) => cache.get(id)).filter((p): p is Product => !!p)
}

export async function fetchProduct(id: string): Promise<Product | null> {
  const hit = cache.get(id)
  if (hit) return hit
  try {
    const r = await fetch(`/api/products/${encodeURIComponent(id)}`, { headers: authHeaders() })
    if (!r.ok) return null
    const data: { product: Product } = await r.json()
    cache.set(data.product.product_id, data.product)
    return data.product
  } catch {
    return null
  }
}

export async function fetchSearch(
  q: string,
  budget?: number,
): Promise<{ products: Product[]; empty: boolean; degraded: boolean }> {
  try {
    const params = new URLSearchParams({ q })
    if (budget) params.set('budget', String(budget))
    const r = await fetch(`/api/search?${params}`, { headers: authHeaders() })
    if (!r.ok) return { products: [], empty: true, degraded: true }
    const data: { products?: Product[]; empty?: boolean; degraded?: boolean } = await r.json()
    for (const p of data.products ?? []) cache.set(p.product_id, p)
    return {
      products: data.products ?? [],
      empty: data.empty ?? !data.products?.length,
      // 后端把「MCP 慢/挂」和「真没货」分开了：降级时给「重试」，别拿「暂时没货」糊弄
      degraded: data.degraded ?? false,
    }
  } catch {
    // 后端都连不上，同样是可重试的降级
    return { products: [], empty: true, degraded: true }
  }
}

/** 给定一批商品 id，返回 id -> 商品 的映射（详情页/卡片用） */
export function useProducts(ids: string[]): Record<string, Product> {
  const key = ids.join(',')
  const [map, setMap] = useState<Record<string, Product>>({})
  useEffect(() => {
    let cancelled = false
    if (!key) {
      setMap({})
      return
    }
    fetchProducts(key.split(','))
      .then((list) => {
        if (cancelled) return
        const m: Record<string, Product> = {}
        for (const p of list) m[p.product_id] = p
        setMap(m)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [key])
  return map
}

// 最近浏览：首页展示用，记本地就行，不用后端
const RECENT_KEY = 'recent_products'

export function pushRecent(productId: string): void {
  try {
    const list: string[] = JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')
    const next = [productId, ...list.filter((id) => id !== productId)].slice(0, 8)
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    /* 隐私模式写不了就算了 */
  }
}

export function readRecent(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')
  } catch {
    return []
  }
}