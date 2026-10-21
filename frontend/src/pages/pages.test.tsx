// C 端页面用例：首页 / 搜索 / 详情 / 订单 / 我的。
// 页面只连 /api 读接口，测试统一 mock fetch；商品缓存在模块内存里，
// 所以每个用例用**不同的商品 id**，避免缓存串场。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import Home from './Home'
import Me from './Me'
import Monitor from './Monitor'
import Orders from './Orders'
import ProductDetail from './ProductDetail'
import Search from './Search'

interface ProductBody {
  product_id: string
  name: string
  category?: string
  brand?: string
  tags?: string[]
  image?: string
  negative_rate?: number | null
  shop_score?: number | null
  platforms?: Record<string, { price: number; url: string; url_kind?: 'product' | 'search' }>
}

function product(over: Partial<ProductBody> & { product_id: string }): ProductBody {
  return {
    name: '小米 Buds 4 Pro 真无线降噪耳机',
    tags: ['降噪', '蓝牙'],
    image: 'https://img/x.png',
    negative_rate: 0.035,
    shop_score: 4.6,
    platforms: {
      京东: { price: 899, url: 'https://search.jd.com/x' },
      拼多多: { price: 799, url: 'https://mobile.yangkeduo.com/x' },
    },
    ...over,
  }
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body))
}

// 路由壳：页面里用了 useNavigate/useParams/useSearchParams，必须包 Router
function renderAt(path: string, ui: ReactElement, routePath: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={routePath} element={ui} />
      </Routes>
    </MemoryRouter>,
  )
}

// 跳转探针：把当前路径显示出来，验证「点了历史对话能进对话页」这类跳转
function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{`${loc.pathname}${loc.search}`}</div>
}

function renderWithProbe(path: string, ui: ReactElement, routePath: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={routePath} element={ui} />
        <Route path="/chat" element={<LocationProbe />} />
        <Route path="/monitor" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.unstubAllGlobals())

describe('首页 Home', () => {
  it('展示 hero、分类入口和 AI 导购入口', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ products: [] })))
    renderAt('/', <Home />, '/')

    expect(screen.getByText(/想买什么/)).toBeInTheDocument()
    expect(screen.getByPlaceholderText('比如：2000 以内的降噪耳机')).toBeInTheDocument()
    for (const cat of ['耳机', '手机', '键盘', '显示器']) {
      expect(screen.getByText(cat)).toBeInTheDocument()
    }
    expect(screen.getByText('AI 导购')).toBeInTheDocument()
  })

  it('有最近浏览时渲染商品卡片', async () => {
    localStorage.setItem('recent_products', JSON.stringify(['home-recent-1']))
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ products: [product({ product_id: 'home-recent-1' })] })),
    )
    renderAt('/', <Home />, '/')

    expect(await screen.findByText(/最近浏览/)).toBeInTheDocument()
    expect(await screen.findByText('小米 Buds 4 Pro 真无线降噪耳机')).toBeInTheDocument()
    expect(screen.getByText('799')).toBeInTheDocument() // 最低价取全网最低
  })
})

describe('搜索页 Search', () => {
  it('按关键词出商品卡片，卡片带图/价/平台标签', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({ products: [product({ product_id: 'search-hit-1' })], empty: false, degraded: false }),
      ),
    )
    renderAt('/search?q=降噪耳机', <Search />, '/search')

    expect(await screen.findByText('小米 Buds 4 Pro 真无线降噪耳机')).toBeInTheDocument()
    expect(screen.getByText(/共 1 件/)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '小米 Buds 4 Pro 真无线降噪耳机' })).toBeInTheDocument()
    expect(screen.getByText('京东')).toBeInTheDocument()
    expect(screen.getByText('拼多多')).toBeInTheDocument()
  })

  it('降级（MCP 慢/挂）时提示可重试，点重试重新请求并出结果', async () => {
    const bodies = [
      { products: [], empty: true, degraded: true },
      { products: [product({ product_id: 'search-retry-1' })], empty: false, degraded: false },
    ]
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json(bodies[Math.min(call++, bodies.length - 1)])),
    )
    const user = userEvent.setup()
    renderAt('/search?q=降噪耳机', <Search />, '/search')

    expect(await screen.findByText(/可能不是真没货/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('小米 Buds 4 Pro 真无线降噪耳机')).toBeInTheDocument()
    expect(call).toBe(2)
  })

  it('真没货时给「没找到」文案 + 重试按钮', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ products: [], empty: true, degraded: false })),
    )
    renderAt('/search?q=冰箱', <Search />, '/search')

    expect(await screen.findByText(/没找到符合条件的商品/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })
})

describe('商品详情 ProductDetail', () => {
  it('渲染大图/最低价/店铺分/差评率 + 多平台比价表（最低价标「全网最低」）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ product: product({ product_id: 'detail-ok-1' }) })),
    )
    renderAt('/product/detail-ok-1', <ProductDetail />, '/product/:id')

    expect(await screen.findByRole('heading', { name: '小米 Buds 4 Pro 真无线降噪耳机' })).toBeInTheDocument()
    // 「全网最低」在详情价签和比价表最低价行各出现一次，比价表那行用 pp-best 定位
    expect(screen.getByText('全网最低', { selector: '.pp-best' })).toBeInTheDocument()
    // 799 在「全网最低价签」和「比价表拼多多那一行」各出现一次
    expect(screen.getAllByText('799')).toHaveLength(2)
    expect(screen.getByText(/4.6/)).toBeInTheDocument() // 店铺评分
    expect(screen.getByText(/3.5%/)).toBeInTheDocument() // 差评率

    // 比价表按价格升序：拼多多 799 在前
    const buyLinks = screen.getAllByRole('link')
    expect(buyLinks[0]).toHaveTextContent('去拼多多购买')
    expect(buyLinks[0]).toHaveAttribute('href', 'https://mobile.yangkeduo.com/x')
    expect(buyLinks[1]).toHaveTextContent('去京东购买')
  })

  it('url_kind=search 的平台按钮写「搜索」，不冒充商品页', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          product: product({
            product_id: 'detail-search-kind-1',
            platforms: {
              京东: { price: 899, url: 'https://search.jd.com/Search?keyword=x', url_kind: 'search' },
              拼多多: { price: 799, url: 'https://mobile.yangkeduo.com/x', url_kind: 'product' },
            },
          }),
        }),
      ),
    )
    renderAt('/product/detail-search-kind-1', <ProductDetail />, '/product/:id')

    const links = await screen.findAllByRole('link')
    expect(links[0]).toHaveTextContent('去拼多多购买') // 精准落地页
    expect(links[1]).toHaveTextContent('去京东搜索') // 搜索页兜底，如实标注
    expect(links[1]).toHaveAttribute('href', 'https://search.jd.com/Search?keyword=x')
  })

  it('查不到时给重试入口，点重试重新拉一次', async () => {
    let call = 0
    const fetchMock = vi.fn(async () => {
      call += 1
      // 第一次后端 404（商品不存在或数据源降级），第二次给到商品
      return call === 1
        ? new Response('not found', { status: 404 })
        : json({ product: product({ product_id: 'detail-retry-1' }) })
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    renderAt('/product/detail-retry-1', <ProductDetail />, '/product/:id')

    expect(await screen.findByText(/这件商品暂时查不到了/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('小米 Buds 4 Pro 真无线降噪耳机')).toBeInTheDocument()
    expect(call).toBe(2)
  })

  it('详情页也有盯价入口，点开弹窗能提交（搜索卡片有的，详情页不该少）', async () => {
    const posted: unknown[] = []
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url) === '/api/monitor' && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)))
        return json({ monitor: { id: 2 }, active: true })
      }
      return json({ product: product({ product_id: 'detail-monitor-1' }) })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderAt('/product/detail-monitor-1', <ProductDetail />, '/product/:id')

    await user.click(await screen.findByRole('button', { name: '盯价 小米 Buds 4 Pro 真无线降噪耳机' }))
    await user.type(screen.getByLabelText('目标价（降到多少提醒你）'), '600')
    await user.click(screen.getByRole('button', { name: '确认盯价' }))

    expect(await screen.findByText(/已开始盯价/)).toBeInTheDocument()
    expect(posted).toEqual([{ product_ids: ['detail-monitor-1'], threshold: 600 }])
  })

  it('大模型不可用时回落本地规则：点「帮我参谋一下」不跳转，卡片下面照样出结论', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/api/advice')) return json({ ok: false, source: 'rules' })
        return json({ product: product({ product_id: 'detail-ask-1' }) })
      }),
    )
    const user = userEvent.setup()
    renderWithProbe('/product/detail-ask-1', <ProductDetail />, '/product/:id')

    await user.click(await screen.findByRole('button', { name: '帮我参谋一下' }))

    // 不跳对话页：跳转探针压根没出现
    expect(screen.queryByTestId('loc')).toBeNull()
    const panel = await screen.findByTestId('advice')
    // 结论直接摆这件商品自己的价格和口碑，不去别处再搜一遍同类
    expect(panel).toHaveTextContent('拼多多 ¥799')
    expect(panel).toHaveTextContent('比京东（¥899）便宜 ¥100.0（约 13%）')
    expect(panel).toHaveTextContent('差评率 3.5%，不高不低')
    expect(panel).toHaveTextContent('店铺评分 4.6 分')
    // 走的是兜底就得如实说，别装作是 AI 分析的
    expect(panel).toHaveTextContent('本地规则兜底')

    // 再点一次收起，别赖在页面上
    await user.click(await screen.findByRole('button', { name: '收起参谋建议' }))
    expect(screen.queryByTestId('advice')).toBeNull()
  })

  it('参谋结论跟着口碑走：差评率高/店铺分低就劝先别买，并说清哪个平台只是搜索页', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/api/advice')) return json({ ok: false, source: 'rules' })
        return json({
          product: product({
            product_id: 'detail-ask-2',
            negative_rate: 0.08,
            shop_score: 3.6,
            platforms: {
              京东: { price: 199, url: 'https://item.jd.com/x', url_kind: 'product' },
              天猫: { price: 219, url: 'https://s.taobao.com/x', url_kind: 'search' },
            },
          }),
        })
      }),
    )
    const user = userEvent.setup()
    renderWithProbe('/product/detail-ask-2', <ProductDetail />, '/product/:id')

    await user.click(await screen.findByRole('button', { name: '帮我参谋一下' }))

    const panel = await screen.findByTestId('advice')
    expect(panel).toHaveTextContent('别急着买')
    expect(panel).toHaveTextContent('差评率 8.0%，偏高')
    expect(panel).toHaveTextContent('店铺评分 3.6 分，偏低')
    // 天猫那条没有直达商品页，点过去只是搜索页，得如实讲
    expect(panel).toHaveTextContent('天猫没有直达这件商品的页面')
  })

  it('参谋正常走大模型：/api/advice 给的分析直接展示，不再露本地规则那套文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/api/advice')) {
          return json({
            ok: true,
            verdict: '京东 ¥9.8 是全网最低，口碑也没硬伤，可以入',
            tone: 'good',
            points: ['京东 ¥9.8 是全网最低', '差评率 1.2%，算低的', '店铺评分 4.8 分，靠谱'],
          })
        }
        return json({ product: product({ product_id: 'detail-ask-3' }) })
      }),
    )
    const user = userEvent.setup()
    renderWithProbe('/product/detail-ask-3', <ProductDetail />, '/product/:id')

    await user.click(await screen.findByRole('button', { name: '帮我参谋一下' }))

    const panel = await screen.findByTestId('advice')
    expect(panel).toHaveTextContent('京东 ¥9.8 是全网最低，口碑也没硬伤，可以入')
    expect(panel).toHaveTextContent('差评率 1.2%，算低的')
    expect(panel).toHaveTextContent('AI 分析')
    // 本地规则那套格式（「各平台：…」）不该再出现
    expect(panel).not.toHaveTextContent('各平台：')
  })
})

describe('订单页 Orders', () => {
  it('空订单时引导去 AI 导购', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ orders: [] })))
    renderAt('/orders', <Orders />, '/orders')

    expect(await screen.findByText(/还没有订单/)).toBeInTheDocument()
    expect(screen.getByText('AI 导购')).toBeInTheDocument()
  })

  it('有订单时渲染商品名/平台/状态 + 可点跳转链接', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          orders: [
            {
              id: 1,
              product_id: 'order-1',
              name: '华为 FreeBuds Pro 3',
              platform: '拼多多',
              price: 1069,
              status: 'intent',
              url: 'https://mobile.yangkeduo.com/y',
            },
          ],
        }),
      ),
    )
    renderAt('/orders', <Orders />, '/orders')

    expect(await screen.findByText('华为 FreeBuds Pro 3')).toBeInTheDocument()
    expect(screen.getByText('待购买')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: '去拼多多购买' })
    expect(link).toHaveAttribute('href', 'https://mobile.yangkeduo.com/y')
  })
})

describe('个人主页 Me', () => {
  it('渲染三统计 + 偏好 chips + 盯价 + 历史对话', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          user_id: 'demo',
          preferences: ['偏爱国产'],
          recent_summaries: [
            { session_id: 's-1', summary: '用户想买 2000 内的降噪耳机', topics: ['降噪耳机'] },
          ],
          order_count: 2,
          monitors: [
            { id: 1, product_ids: ['m1'], names: ['小米耳机'], threshold: 800, active: true },
          ],
        }),
      ),
    )
    renderAt('/me', <Me />, '/me')

    expect(await screen.findByText('偏爱国产')).toBeInTheDocument()
    expect(screen.getByText('我的订单')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('小米耳机')).toBeInTheDocument()
    expect(screen.getByText('监控中')).toBeInTheDocument()
    expect(screen.getByText('用户想买 2000 内的降噪耳机')).toBeInTheDocument()
  })

  it('点历史对话进对话页，带上 session 继续聊', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          user_id: 'demo',
          preferences: [],
          recent_summaries: [{ session_id: 's-abc', summary: '聊过降噪耳机' }],
          order_count: 0,
          monitors: [],
        }),
      ),
    )
    const user = userEvent.setup()
    renderWithProbe('/me', <Me />, '/me')

    const card = await screen.findByRole('button', { name: /聊过降噪耳机/ })
    await user.click(card)

    expect(screen.getByTestId('loc')).toHaveTextContent('/chat?session=s-abc')
  })

  it('点「盯价页」入口进 /monitor', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({ user_id: 'demo', preferences: [], recent_summaries: [], order_count: 0, monitors: [] }),
      ),
    )
    const user = userEvent.setup()
    renderWithProbe('/me', <Me />, '/me')

    await user.click(await screen.findByRole('button', { name: /看价格走势/ }))
    expect(screen.getByTestId('loc')).toHaveTextContent('/monitor')
  })

  it('盯价卡片不把整批商品名铺成一屏，超两条折成「等 N 件」', async () => {
    const names = Array.from({ length: 30 }, (_, i) => `耳机${i + 1}`)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          user_id: 'demo',
          preferences: [],
          recent_summaries: [],
          order_count: 0,
          monitors: [{ id: 9, product_ids: names, names, threshold: 300, active: true }],
        }),
      ),
    )
    renderAt('/me', <Me />, '/me')

    expect(await screen.findByText('耳机1、耳机2 等 30 件')).toBeInTheDocument()
    expect(screen.queryByText(/耳机7、耳机8/)).not.toBeInTheDocument()
  })

  it('偏好为空时给灰态提示', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({ user_id: 'demo', preferences: [], recent_summaries: [], order_count: 0, monitors: [] }),
      ),
    )
    renderAt('/me', <Me />, '/me')

    await waitFor(() => expect(screen.getByText(/还没有沉淀出偏好/)).toBeInTheDocument())
  })

  it('点「取消」调 DELETE 盯价接口，成功后从列表移除', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') return json({ ok: true, monitor_id: 1 })
      return json({
        user_id: 'demo',
        preferences: [],
        recent_summaries: [],
        order_count: 0,
        monitors: [{ id: 1, product_ids: ['m1'], names: ['小米耳机'], threshold: 800, active: true }],
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderAt('/me', <Me />, '/me')

    expect(await screen.findByText('小米耳机')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '取消盯价 小米耳机' }))

    await waitFor(() => expect(screen.queryByText('小米耳机')).not.toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/monitor/1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})

describe('盯价页 Monitor', () => {
  it('列出盯价订阅，展开后按商品画价格走势', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (String(url).includes('/history')) {
        return json({
          monitor_id: 1,
          threshold: 800,
          series: [
            {
              product_id: 'm1',
              name: '小米耳机',
              points: [
                { platform: '京东', price: 899, at: '2026-10-01T00:00:00Z' },
                { platform: '京东', price: 799, at: '2026-10-02T00:00:00Z' },
                { platform: '拼多多', price: 780, at: '2026-10-03T00:00:00Z' },
              ],
            },
          ],
        })
      }
      if (String(url).includes('/notify')) return json({ notifications: [] })
      return json({
        monitors: [
          { id: 1, product_ids: ['m1'], names: ['小米耳机'], threshold: 800, active: true },
        ],
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderAt('/monitor', <Monitor />, '/monitor')

    expect(await screen.findByText('小米耳机')).toBeInTheDocument()
    expect(screen.getByText('目标价 ¥800')).toBeInTheDocument()
    expect(screen.getByText('监控中')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '价格走势' }))

    // 已达目标价（最近 780 ≤ 800）要说出来，并画出两条平台折线
    expect(await screen.findByText(/最近 ¥780（已达目标价）/)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '价格走势' })).toBeInTheDocument()
    expect(screen.getByText(/京东 最低 ¥799/)).toBeInTheDocument()
    expect(screen.getByText(/拼多多 最低 ¥780/)).toBeInTheDocument()
  })

  it('没盯商品时引导去搜货', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        String(url).includes('/notify') ? json({ notifications: [] }) : json({ monitors: [] }),
      ),
    )
    renderAt('/monitor', <Monitor />, '/monitor')

    expect(await screen.findByText(/还没有在盯的商品/)).toBeInTheDocument()
  })
})

describe('商品卡片盯价入口', () => {
  it('点「盯价」弹窗填目标价，确认后 POST /api/monitor', async () => {
    const posted: unknown[] = []
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url) === '/api/monitor' && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)))
        return json({ monitor: { id: 1 }, active: true })
      }
      return json({ products: [product({ product_id: 'card-monitor-1' })], empty: false, degraded: false })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderAt('/search?q=耳机', <Search />, '/search')

    await user.click(await screen.findByRole('button', { name: '盯价 小米 Buds 4 Pro 真无线降噪耳机' }))
    const input = screen.getByLabelText('目标价（降到多少提醒你）')
    // 不给默认值：默认填现价（799）等于默认给个「已经达标」的目标价，一点确认就必推提醒
    expect(input).toHaveValue('')

    await user.type(input, '600')
    await user.click(screen.getByRole('button', { name: '确认盯价' }))

    expect(await screen.findByText(/已开始盯价/)).toBeInTheDocument()
    expect(posted).toEqual([{ product_ids: ['card-monitor-1'], threshold: 600 }])
  })

  it('填的价比现价还高也照提交：后端按现价当基准盯，比现价便宜才叫你', async () => {
    const posted: { threshold?: number }[] = []
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url) === '/api/monitor' && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)))
        return json({ monitor: { id: 1 }, active: true, below: 1 })
      }
      return json({ products: [product({ product_id: 'card-monitor-2' })], empty: false, degraded: false })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderAt('/search?q=耳机', <Search />, '/search')

    await user.click(await screen.findByRole('button', { name: '盯价 小米 Buds 4 Pro 真无线降噪耳机' }))
    await user.type(screen.getByLabelText('目标价（降到多少提醒你）'), '900')
    await user.click(screen.getByRole('button', { name: '确认盯价' }))

    expect(await screen.findByText(/已开始盯价/)).toBeInTheDocument()
    expect(posted).toEqual([{ product_ids: ['card-monitor-2'], threshold: 900 }])
  })
})