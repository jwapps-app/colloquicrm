import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// public/sw.js is copied to dist verbatim, so it can't see Vite `define`s.
// This stamps it after the build with an id derived from the hashed output
// file names: a new build (new hashes) gets new cache names, and the worker's
// `activate` deletes the old ones. Identical output → identical id.
function stampServiceWorker() {
  const PLACEHOLDER = '__CRM_BUILD_ID__';
  let buildId = '';
  let outDir = 'dist';
  return {
    name: 'crm-stamp-service-worker',
    apply: 'build',
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir);
    },
    generateBundle(_options, bundle) {
      const names = Object.keys(bundle).sort().join('\n');
      buildId = createHash('sha256').update(names).digest('hex').slice(0, 12);
    },
    closeBundle() {
      const swPath = resolve(outDir, 'sw.js');
      if (!buildId || !existsSync(swPath)) return;
      const src = readFileSync(swPath, 'utf8');
      if (!src.includes(PLACEHOLDER)) {
        this.warn('sw.js has no build-id placeholder — caches will not be versioned');
        return;
      }
      writeFileSync(swPath, src.replace(PLACEHOLDER, buildId));
    },
  };
}

export default defineConfig({
  plugins: [react(), stampServiceWorker()],
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
