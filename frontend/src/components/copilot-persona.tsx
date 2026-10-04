import { Pause, Play, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import { mascotState, statusLabel } from "@/lib/copilot-status";
import {
  usePauseCopilot,
  useResumeCopilot,
  useCopilotProfile,
  type CopilotAvatar as CopilotAvatarDTO,
} from "@/lib/hooks-copilot";

type MascotState = "idle" | "working" | "waiting" | "paused";

const COLORS: Record<CopilotAvatarDTO["color"], string> = {
  indigo: "#6366f1",
  teal: "#14b8a6",
  amber: "#f59e0b",
  rose: "#f43f5e",
  slate: "#64748b",
  lime: "#84cc16",
};

function Body({ shape, fill }: { shape: CopilotAvatarDTO["shape"]; fill: string }) {
  switch (shape) {
    case "square":
      return <rect x="5" y="5" width="30" height="30" rx="7" fill={fill} />;
    case "drop":
      return <path d="M20 3 C28 14 34 20 34 26 A14 14 0 0 1 6 26 C6 20 12 14 20 3 Z" fill={fill} />;
    case "star":
      return (
        <path
          d="M20 3 L24.5 14 L36 15 L27.2 22.6 L30 34 L20 27.8 L10 34 L12.8 22.6 L4 15 L15.5 14 Z"
          fill={fill}
        />
      );
    default:
      return <circle cx="20" cy="20" r="16" fill={fill} />;
  }
}

/** Pure-SVG persona: shape x colour with eyes; the state only changes motion/overlay. */
export function CopilotAvatar({
  avatar,
  state,
  size = 32,
}: {
  avatar: CopilotAvatarDTO;
  state: MascotState;
  size?: number;
}) {
  const fill = COLORS[avatar.color] ?? COLORS.indigo;
  const eyeY = avatar.shape === "drop" || avatar.shape === "star" ? 22 : 19;
  return (
    <span
      data-avatar-state={state}
      className={cn(
        "relative inline-block shrink-0",
        state === "working" && "motion-safe:animate-pulse",
        state === "paused" && "opacity-50 grayscale",
      )}
      style={{ width: size, height: size }}
    >
      <svg viewBox="0 0 40 40" width={size} height={size} aria-hidden="true">
        <Body shape={avatar.shape} fill={fill} />
        {state === "paused" ? (
          <g stroke="#fff" strokeWidth="2" strokeLinecap="round">
            <path d={`M13 ${eyeY} h4`} />
            <path d={`M23 ${eyeY} h4`} />
          </g>
        ) : (
          <g fill="#fff">
            <circle cx="14.5" cy={eyeY} r="2.4" />
            <circle cx="25.5" cy={eyeY} r="2.4" />
          </g>
        )}
      </svg>
      {state === "waiting" && (
        <span
          data-testid="avatar-waiting-dot"
          className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-background bg-amber-500 motion-safe:animate-pulse"
        />
      )}
    </span>
  );
}

export function CopilotPersonaHeader({
  variant,
  onOpenProfile,
  children,
}: {
  variant: "dock" | "page";
  onOpenProfile?: () => void;
  children?: ReactNode;
}) {
  const t = useT();
  const { data: profile } = useCopilotProfile();
  const pause = usePauseCopilot();
  const resume = useResumeCopilot();

  const status = profile?.status ?? "ready";
  const state = mascotState(status);
  const avatar: CopilotAvatarDTO = profile?.avatar ?? { shape: "round", color: "indigo" };
  const name = profile?.displayName ?? "Copilot";
  const paused = profile?.pausedAt != null;
  const page = variant === "page";

  return (
    <div className={cn("flex items-center gap-2.5", page && "gap-3")}>
      <CopilotAvatar avatar={avatar} state={state} size={page ? 40 : 32} />
      <div className="min-w-0 flex-1 leading-tight">
        <div className={cn("truncate font-serif", page ? "text-lg" : "text-base")}>{name}</div>
        <div className="text-[11px] text-muted-foreground">
          {statusLabel(t, status, profile?.activeCount ?? 0)}
        </div>
      </div>
      {page && (
        <>
          <button
            type="button"
            onClick={() => (paused ? resume.mutate() : pause.mutate())}
            disabled={pause.isPending || resume.isPending}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
          >
            {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
            {paused ? t("Resume", "Fortsetzen") : t("Pause", "Pausieren")}
          </button>
          <button
            type="button"
            onClick={onOpenProfile}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:text-foreground"
          >
            <UserRound className="h-3.5 w-3.5" />
            {t("Profile", "Profil")}
          </button>
        </>
      )}
      {children}
    </div>
  );
}
