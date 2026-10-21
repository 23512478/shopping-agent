// 统一给 fetch 加认证头。VITE_API_TOKEN 不配时返回空对象，本地开发不受影响。
const TOKEN = import.meta.env.VITE_API_TOKEN as string | undefined
const USER_TOKEN_KEY = 'sa_user_token'

// 匿名身份：首次访问领一个 token 存本地，之后所有请求带着它，后端据此认出是谁
let userToken: string | null = localStorage.getItem(USER_TOKEN_KEY)

/** 领匿名身份。没网 / 后端没起也不拦页面渲染，接口会退回 demo 兜底身份。 */
export async function initIdentity(timeoutMs = 3000): Promise<void> {
  // 加超时上限：后端接了连接却不回时别把首屏卡死
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    // 带上 authHeaders()：生产配了 API_TOKEN 时这请求才不会被拦，旧 token 也顺带复用
    const r = await fetch('/api/auth/anon', {
      method: 'POST',
      headers: authHeaders(),
      signal: ctrl.signal,
    })
    if (!r.ok) return
    const d = (await r.json()) as { token?: string }
    if (d?.token) {
      userToken = d.token
      localStorage.setItem(USER_TOKEN_KEY, d.token)
    }
  } catch {
    // 离线 / 后端没起 / 超时：不拦渲染，页面自己的请求走 demo 兜底
  } finally {
    clearTimeout(timer)
  }
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`
  if (userToken) headers['X-User-Token'] = userToken
  return headers
}