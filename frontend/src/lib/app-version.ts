// The app's version from package.json, set at build time by the `define` in
// vite.config.ts (and vitest.config.ts). Empty when a build did not set it.
export const APP_VERSION: string = import.meta.env.VITE_APP_VERSION ?? "";
