import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发模式下 /api 代理到本地后端；生产构建走后端静态托管，同源无需代理
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8710",
        changeOrigin: false,
      },
    },
  },
});
