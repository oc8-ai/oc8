import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

// Any locale a `.po` catalog fetched from `/i18n/core` reports, plus the two
// built into the source itself: "en" (every `t()` call's first argument,
// needing no lookup) and "de" (every call's second argument, present in
// every call site regardless of whether `i18n/de.po` exists on disk).
export type Lang = string;

const CORE_I18N_URL =
  (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8099/api/v1";

export interface LocaleInfo {
  code: string;
  nativeName: string;
  flag: string;
}

const BUILTIN_LOCALES: LocaleInfo[] = [
  { code: "en", nativeName: "English", flag: "🇬🇧" },
  { code: "de", nativeName: "Deutsch", flag: "🇩🇪" },
];

type CoreCatalogs = Record<string, Record<string, string>>;

interface CoreI18nResponse {
  locales: {
    locale: string;
    nativeName: string;
    flag: string;
    translations: Record<string, string>;
  }[];
}

interface LangCtx {
  lang: Lang;
  setLang: (l: Lang) => void;
  /** Built-in en/de plus every locale `/i18n/core` reports, deduped by code. */
  locales: LocaleInfo[];
  /** `{locale: {msgid: msgstr}}`, empty until the one startup fetch resolves. */
  catalogs: CoreCatalogs;
}

const Ctx = createContext<LangCtx>({
  lang: "en",
  setLang: () => {},
  locales: BUILTIN_LOCALES,
  catalogs: {},
});

/** Built-in en/de plus each reported locale that carries at least one string
 *  (the server already omits empty ones; this keeps the picker honest against
 *  an older backend). Deduped by code, reported entries win. */
export function mergeLocales(reported: CoreI18nResponse["locales"]): LocaleInfo[] {
  const byCode = new Map(BUILTIN_LOCALES.map((l) => [l.code, l]));
  for (const l of reported) {
    if (Object.keys(l.translations ?? {}).length === 0) continue;
    byCode.set(l.locale, { code: l.locale, nativeName: l.nativeName || l.locale, flag: l.flag });
  }
  return [...byCode.values()];
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>("en");
  const [locales, setLocales] = useState<LocaleInfo[]>(BUILTIN_LOCALES);
  const [catalogs, setCatalogs] = useState<CoreCatalogs>({});

  // Fetched once per page load, before login: the login screen itself is
  // translated. A failed fetch just means every language falls back to its
  // in-source literal (English always, German via its own `de` argument) --
  // see `useT` below.
  useEffect(() => {
    fetch(`${CORE_I18N_URL}/i18n/core`)
      .then((r) => r.json() as Promise<CoreI18nResponse>)
      .then((data) => {
        setLocales(mergeLocales(data.locales));
        setCatalogs(Object.fromEntries(data.locales.map((l) => [l.locale, l.translations])));
      })
      .catch(() => {
        /* keep the built-in en/de entries and empty catalogs */
      });
  }, []);

  // Read localStorage after mount to avoid SSR hydration mismatch.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem("oc8-lang");
      if (stored) setLangState(stored);
    } catch {
      /* noop */
    }
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("oc8-lang", lang);
      document.documentElement.lang = lang;
    } catch {
      /* noop */
    }
  }, [lang]);

  const value = useMemo(
    () => ({ lang, setLang: setLangState, locales, catalogs }),
    [lang, locales, catalogs],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLang() {
  return useContext(Ctx);
}

/**
 * Inline translator. Usage: `t("Departments", "Abteilungen")`.
 *
 * `en` is the source text and always the fallback of last resort. `de` is a
 * literal, present at every call site regardless of whether `i18n/de.po`
 * exists on disk -- German works even with an empty or missing catalog.
 * A catalog entry for the CURRENT language, when one is loaded (from
 * `/i18n/core`, fetched once at startup), always wins over both: it is how
 * a `.po` file overrides or corrects a hardcoded string without a code
 * change, and it is the ONLY source of truth for any language besides
 * English and German, which have no second literal argument to fall back to.
 */
export function useT() {
  const { lang, catalogs } = useContext(Ctx);
  return useCallback(
    (en: string, de?: string) => {
      if (lang === "en") return en;
      if (lang === "de") return catalogs["de"]?.[en] ?? de ?? en;
      return catalogs[lang]?.[en] ?? en;
    },
    [lang, catalogs],
  );
}

/**
 * Resolves a capa-authored English string against the `<field>Translations`
 * map the backend bulk-resolves from that capa's `i18n/*.po` catalogs
 * (capa-i18n design). A locale missing from the map -- untranslated, or a
 * locale the capa never shipped -- falls back to `source` itself.
 */
export function resolveTranslation(
  source: string,
  translations: Record<string, string> | undefined,
  lang: Lang,
): string {
  if (lang === "en" || !translations) return source;
  return translations[lang] ?? source;
}
