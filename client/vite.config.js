import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Ports are configurable so this app can run alongside other local services:
//   API_PORT=8010 WEB_PORT=5180 npm run dev
const apiPort = process.env.API_PORT || '8000';
const apiTarget = process.env.API_TARGET || `http://127.0.0.1:${apiPort}`;
const webPort = Number(process.env.WEB_PORT || 5173);

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [react(), tailwindcss()],
  server: {
    port: webPort,
    strictPort: true,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
});
