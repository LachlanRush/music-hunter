import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Built into the Python package's static folder, so the add-on and `music-hunter pi push` need no build step;
// served by the app at /next (index.html) and /static/app/ (its assets).
export default defineConfig({
  plugins: [svelte()],
  base: "/static/app/",
  build: { outDir: "../src/music_hunter/static/app", emptyOutDir: true, target: "es2022", sourcemap: false },
  server: { proxy: { "^/(api|audio|art|tempo|find|static/(?!app))": "http://127.0.0.1:8765" } },
  test: { environment: "node" },
});
