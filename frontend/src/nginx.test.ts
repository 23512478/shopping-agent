/// <reference types="vite/client" />
// 部署层守门：前端容器的 nginx.conf 必须把 /api 转给后端。
// 第三十五轮踩过这个坑——少了那段代理，页面的 /api/* 会被 SPA 兜底回 index.html，
// 页面拿 HTML 当 JSON 解析，结果整页空白。配置文件是纯文本，这里直接读断言，不用起容器。
import { describe, expect, it } from 'vitest'
import conf from '../nginx.conf?raw'

function block(name: string): string {
  // 取 `location <name> { ... }` 这一段；这几段的配置项里没有嵌套花括号，非贪婪到第一个 } 就够
  return conf.match(new RegExp(`location\\s+${name}\\s*\\{[\\s\\S]*?\\}`))?.[0] ?? ''
}

describe('frontend/nginx.conf', () => {
  it('有 /api 代理，别让 SPA 兜底吃掉接口请求', () => {
    const api = block('/api/')
    expect(api, 'nginx.conf 里找不到 location /api/ 段').not.toBe('')
    expect(api).toContain('proxy_pass http://backend:8000')
  })

  it('接口响应与兜底页面都标 no-store，防止旧 HTML 被缓存在接口 URL 上', () => {
    expect(block('/api/')).toContain('no-store')
    expect(block('/')).toContain('no-store')
  })

  it('SPA 路由兜底还在（直接访问 /search、/product/:id 刷新不能 404）', () => {
    expect(block('/')).toContain('try_files $uri $uri/ /index.html')
  })
})
