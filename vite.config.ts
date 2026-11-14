import { defineConfig } from 'vite';

// base './' so the build works from https://davefroriep.github.io/armory/ and from any sub-path.
export default defineConfig({
  base: './',
  build: { target: 'es2022', sourcemap: false },
  test: { environment: 'node', include: ['tests/**/*.test.ts'] },
});
