// The two screens build to static files the daybook server hands out from an
// in-memory map. `base` is "/app/" because the server serves the bundle under
// that prefix while the screens themselves live at /insights and /activities --
// the app router decides which screen to draw, so both routes return the same
// index.html and neither is a file on disk.
import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react-swc';

export default defineConfig({
  plugins: [react()],
  root: 'web',
  base: '/app/',
  build: {
    outDir: '../server/assets/app',
    emptyOutDir: true,
    // The server caches hashed filenames immutably, so the hash has to be in
    // the name. Vite does this by default; it is stated here because the
    // caching header on the other side depends on it.
    assetsDir: 'assets',
    sourcemap: false,
  },
});
