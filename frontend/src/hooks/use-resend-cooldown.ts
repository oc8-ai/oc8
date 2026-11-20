// frontend/src/hooks/use-resend-cooldown.ts
import { useEffect, useState } from "react";
import { RESEND_COOLDOWN_SECONDS } from "@/lib/two-factor";

/** Seconds left before another code may be requested; `start()` after each
 *  send. Mirrors the server's one-code-a-minute rule so the button does not
 *  invite a request that would only be refused. */
export function useResendCooldown() {
  const [until, setUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (until === null) return;
    const id = setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= until) setUntil(null);
    }, 1000);
    return () => clearInterval(id);
  }, [until]);
  const remaining = until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
  return {
    remaining,
    start: () => {
      const current = Date.now();
      setNow(current);
      setUntil(current + RESEND_COOLDOWN_SECONDS * 1000);
    },
  };
}
