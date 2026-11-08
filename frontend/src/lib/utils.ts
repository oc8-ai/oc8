import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// `text-micro` is the 11px step of the type scale (styles.css @theme). Without
// registering it, tailwind-merge would read it as a text *colour* and drop it
// next to `text-muted-foreground` (or vice versa).
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["micro"] }],
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
