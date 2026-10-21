import { Navigate, Route, Routes } from 'react-router-dom'
import NavBar from './components/NavBar'
import AdminDeadLetter from './pages/AdminDeadLetter'
import Chat from './pages/Chat'
import Home from './pages/Home'
import Me from './pages/Me'
import Monitor from './pages/Monitor'
import Orders from './pages/Orders'
import ProductDetail from './pages/ProductDetail'
import Search from './pages/Search'

// 全站骨架：全局导航 + 路由。失败任务只在 /admin 下，不进任何 C 端入口。
export default function App() {
  return (
    <>
      {/* 毛玻璃光球背景 */}
      <div className="orbs-bg">
        <div className="orb orb-1" />
        <div className="orb orb-2" />
        <div className="orb orb-3" />
      </div>

      <div className="app-shell">
        <NavBar />
        <div className="page-area">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/search" element={<Search />} />
            <Route path="/product/:id" element={<ProductDetail />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/monitor" element={<Monitor />} />
            <Route path="/me" element={<Me />} />
            <Route path="/admin/deadletter" element={<AdminDeadLetter />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </div>
    </>
  )
}