import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 同源代理避免要求后端放宽 CORS；生产环境由反向代理转发 /api。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': { target: process.env.ETL_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  preview: { port: 4173 },
})
