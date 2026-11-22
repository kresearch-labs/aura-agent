import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  base: "./",
  build: {
    // : the HUB'S served tree (path wins over E.9's dist)
    outDir: "../src/aura_agent/webui/team",
    emptyOutDir: true,
    sourcemap: false,
    assetsInlineLimit: 0,          // no data: inlining surprises; everything is a file
    rollupOptions: {
      input: { team: resolve(__dirname, "team.html") },
    },
  },
  server: { port: 5199, strictPort: true, proxy: { "/api": "https://127.0.0.1:7443" } },
});
