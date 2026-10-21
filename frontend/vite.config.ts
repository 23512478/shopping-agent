/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 本地开发把 /api 转给后端，省得配跨域。
      // 默认转 localhost:8000（docker dev profile 或 python run.py 本地联调）。
      // 如果后端是 prod profile 起的（nginx 唯一入口、8000 没映射），这样跑：
      //   VITE_PROXY_TARGET=https://localhost npm run dev
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        secure: false, // https 目标时放行自签证书
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
  },
})
