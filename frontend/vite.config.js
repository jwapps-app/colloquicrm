import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // Dev backend; override when the API runs elsewhere (e.g. a harness).
        target: process.env.CRM_API_PROXY || 'http://localhost:8010',
        changeOrigin: true,
      },
    },
  },
});
