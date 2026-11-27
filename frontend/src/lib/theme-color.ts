// The browser chrome colour (meta theme-color), in the page's own background
// (styles.css --background). The document ships a light/dark pair keyed to
// the OS scheme; once the app shell knows the theme the member picked, both
// tags take that theme's colour.

export const THEME_COLORS = { light: "#f6f8fa", dark: "#060b14" } as const;

export function syncThemeColor(theme: keyof typeof THEME_COLORS): void {
  if (typeof document === "undefined") return;
  for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    meta.content = THEME_COLORS[theme];
  }
}
