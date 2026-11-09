import * as React from "react";

const MOBILE_BREAKPOINT = 768;

export function useIsMobile() {
  const [isMobile, setIsMobile] = React.useState<boolean | undefined>(undefined);

  React.useEffect(() => {
    const mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`);
    const onChange = () => {
      setIsMobile(window.innerWidth < MOBILE_BREAKPOINT);
    };
    mql.addEventListener("change", onChange);
    setIsMobile(window.innerWidth < MOBILE_BREAKPOINT);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return !!isMobile;
}

const NARROW_BREAKPOINT = 640;

/** Below 640 px (the `sm` breakpoint): where side columns become sheets. */
export function useIsNarrow() {
  const query = `(max-width: ${NARROW_BREAKPOINT - 1}px)`;
  const [narrow, setNarrow] = React.useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
  );

  React.useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const onChange = () => setNarrow(mql.matches);
    mql.addEventListener("change", onChange);
    onChange();
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return narrow;
}
