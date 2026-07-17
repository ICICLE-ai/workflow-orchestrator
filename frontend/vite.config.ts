import { reactRouter } from "@react-router/dev/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [tailwindcss(), reactRouter()],
  resolve: {
    tsconfigPaths: true,
  },
  // Exclude the OpenCV playground + opencv-js from dependency pre-bundling. Its
  // dist does `import cvScriptUrl from "@techstark/opencv-js/dist/opencv.js?url"`,
  // and the optimizer (rolldown/esbuild) can't resolve `?url` asset imports —
  // which caused both "Failed to fetch dynamically imported module" and the
  // UNLOADABLE_DEPENDENCY optimize error. Excluding them serves the package
  // through Vite's normal transform pipeline, where `?url` is handled and the
  // 11MB opencv.wasm loads at runtime.
  optimizeDeps: {
    exclude: [
      "@icicle-ai/opencv-image-playground",
      "@icicle-ai/opencv-image-playground-core",
      "@techstark/opencv-js",
    ],
  },
});
