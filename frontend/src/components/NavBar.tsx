import { NavLink } from 'react-router-dom'

// 导航结构：桌面走顶部，移动端走底部 Tab。图标用内联 SVG，不引图标库。
const LINKS = [
  {
    to: '/',
    label: '首页',
    path: 'M3 11l9-8 9 8v9a2 2 0 0 1-2 2h-4v-6H9v6H5a2 2 0 0 1-2-2z',
  },
  {
    to: '/chat',
    label: 'AI导购',
    path: 'M4 4h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z',
  },
  {
    to: '/orders',
    label: '订单',
    path: 'M3 7l9-4 9 4v10l-9 4-9-4z M3 7l9 4 9-4 M12 11v10',
  },
  {
    to: '/monitor',
    label: '盯价',
    path: 'M12 21a9 9 0 1 0-9-9 M12 7v5l3 2 M21 21l-4-4',
  },
  {
    to: '/me',
    label: '我的',
    path: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 20c0-4 4-6 8-6s8 2 8 6',
  },
]

function Icon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      {path.split(' M').map((seg, i) => (
        <path key={i} d={i === 0 ? seg : `M${seg}`} />
      ))}
    </svg>
  )
}

export default function NavBar() {
  return (
    <>
      <header className="topbar">
        <NavLink to="/" className="brand">
          <span className="brand-mark">购</span>
          <span className="brand-text">
            购物决策 Agent
            <em>AI 帮你搜货 · 比价 · 下单</em>
          </span>
        </NavLink>
        <nav className="topnav">
          {LINKS.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.to === '/'}
              className={({ isActive }) => `topnav-link ${isActive ? 'active' : ''}`}
            >
              <Icon path={l.path} />
              <span>{l.label}</span>
            </NavLink>
          ))}
        </nav>
      </header>

      <nav className="tabbar">
        {LINKS.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            end={l.to === '/'}
            className={({ isActive }) => `tabbar-link ${isActive ? 'active' : ''}`}
          >
            <Icon path={l.path} />
            <span>{l.label}</span>
          </NavLink>
        ))}
      </nav>
    </>
  )
}