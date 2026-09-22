import { Blobatar } from "@blobatar/react";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

type BlobatarBackground = ComponentProps<typeof Blobatar>["background"];

/**
 * An agent's "photo": a deterministic blobatar seeded from a stable value
 * (normally the agent id, so the picture survives a rename). Renders the
 * same creature everywhere the same seed is used, no storage required.
 *
 * `background` picks the backdrop shape and defaults to a circle; pass
 * "squircle" at call sites that previously used a rounded-square swatch.
 *
 * Pass `ariaHidden` (and skip `title`) where the agent's name is already
 * visible as text right next to the avatar, so the picture doesn't get
 * announced a second time.
 */
export function AgentAvatar({
  seed,
  size,
  background = "circle",
  hue,
  title,
  ariaHidden,
  className,
}: {
  seed: string;
  size: number;
  background?: BlobatarBackground;
  hue?: number;
  title?: string;
  ariaHidden?: boolean;
  className?: string;
}) {
  return (
    <Blobatar
      name={seed}
      size={size}
      background={background}
      hue={hue}
      title={ariaHidden ? undefined : title}
      aria-hidden={ariaHidden || undefined}
      className={cn("shrink-0", className)}
    />
  );
}

/**
 * Pulls the numeric hue out of an `oklch(L C H)` string (e.g. the
 * `AVATAR_COLORS` presets), so a picked color can still drive Blobatar's
 * `hue` prop in creation flows that pick a color before an agent exists.
 */
export function hueFromOklch(oklch: string): number | undefined {
  const match = /oklch\([^)]*\s([\d.]+)\)/.exec(oklch);
  return match ? Number(match[1]) : undefined;
}
