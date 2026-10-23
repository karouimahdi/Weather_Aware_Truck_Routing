import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    // maplibre-gl creates its tile-parsing web worker via a relative URL
    // resolved against its own module location. Vite's dev-time dependency
    // pre-bundling serves it from node_modules/.vite/deps/ instead, which
    // breaks that URL ("Worker failed to load") and leaves the map blank.
    // Excluding it from pre-bundling serves the real package path instead.
    exclude: ['maplibre-gl'],
  },
})
