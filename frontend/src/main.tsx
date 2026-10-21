import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { initIdentity } from './api'
import './index.css'

// 先把匿名身份领好再渲染：页面第一个请求就得带上身份，否则会落到 demo 兜底
initIdentity().finally(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </React.StrictMode>,
  )
})