// The zone an agent schedule is read in. The backend reads each cron trigger
// in its own IANA zone (triggers/service.compute_next_run); a trigger saved
// before triggers carried one has none and fires in UTC. A new schedule
// defaults to the organisation's zone, which /me names.
import { useAuth } from "@/lib/hooks";

/** The zone a saved schedule fires in: its own, or UTC when it has none. */
export function scheduleZone(timezone: string | null | undefined): string {
  return timezone || "UTC";
}

/** This browser's own zone, for when the server does not name one. */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Every IANA zone this browser knows, for the picker. UTC first: it is not
 *  always in the list, and it is the one every old schedule uses. */
export function timezoneOptions(): string[] {
  let zones: string[] = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    zones = [];
  }
  return ["UTC", ...zones.filter((z) => z !== "UTC")];
}

/** The organisation's zone (the default for a new schedule), falling back to
 *  this browser's while /me loads or on a backend that does not send it. */
export function useOrgTimezone(): string {
  const { data } = useAuth();
  return data?.organisationTimezone || browserTimezone();
}

/** True once /me says the organisation's zone is still UTC (the default
 *  nobody chose): the schedule editor then points to the settings. */
export function useOrgTimezoneIsUnset(): boolean {
  const { data } = useAuth();
  return !!data && (data.organisationTimezone || "UTC") === "UTC";
}
