import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
  type UseMutationResult,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { RunDTO } from "@/lib/hooks";
import type { CopilotStatus } from "@/lib/copilot-status";

export type { CopilotStatus } from "@/lib/copilot-status";

export interface CopilotAvatar {
  shape: "round" | "square" | "drop" | "star";
  color: "indigo" | "teal" | "amber" | "rose" | "slate" | "lime";
}

export interface CopilotProfileDTO {
  displayName: string;
  avatar: CopilotAvatar;
  pausedAt: string | null;
  status: CopilotStatus;
  activeCount: number;
  agentId: string;
}

export interface ResponsibilityDTO {
  id: string;
  title: string;
  goal: string;
  state: "active" | "waiting" | "paused" | "done" | "cancelled";
  nextStep: string;
  notifyRule: string;
  originChannel: string | null;
  lastUpdateAt: string | null;
  createdAt: string;
  chatSessionId: string;
}

export interface FollowupDTO {
  id: string;
  responsibilityId: string;
  responsibilityTitle: string;
  kind: "once" | "cron";
  cronExpression: string | null;
  timezone: string | null;
  nextRunAt: string | null;
  endsAt: string | null;
  enabled: boolean;
  lastSkipReason: string | null;
  purpose: "check_in" | "research";
}

export interface CopilotNoteDTO {
  id: string;
  content: string;
  createdAt: string;
  responsibilityId: string | null;
  responsibilityTitle: string | null;
}

export const copilotKeys = {
  profile: ["copilot", "profile"] as const,
  responsibilities: ["copilot", "responsibilities"] as const,
  followups: ["copilot", "followups"] as const,
  delegations: ["copilot", "delegations"] as const,
  notes: ["copilot", "notes"] as const,
};

export function useCopilotProfile(enabled = true): UseQueryResult<CopilotProfileDTO> {
  return useQuery({
    queryKey: copilotKeys.profile,
    enabled,
    queryFn: () => api.get<CopilotProfileDTO>("/copilot/profile"),
  });
}

export function useUpdateCopilotProfile(): UseMutationResult<
  CopilotProfileDTO,
  Error,
  { displayName?: string; avatar?: CopilotAvatar }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data) => api.patch<CopilotProfileDTO>("/copilot/profile", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: copilotKeys.profile });
    },
  });
}

export function usePauseCopilot(): UseMutationResult<CopilotProfileDTO, Error, void> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<CopilotProfileDTO>("/copilot/pause", {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: copilotKeys.profile });
      qc.invalidateQueries({ queryKey: copilotKeys.responsibilities });
      qc.invalidateQueries({ queryKey: copilotKeys.followups });
    },
  });
}

export function useResumeCopilot(): UseMutationResult<CopilotProfileDTO, Error, void> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<CopilotProfileDTO>("/copilot/resume", {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: copilotKeys.profile });
      qc.invalidateQueries({ queryKey: copilotKeys.responsibilities });
      qc.invalidateQueries({ queryKey: copilotKeys.followups });
    },
  });
}

export function useResponsibilities(): UseQueryResult<ResponsibilityDTO[]> {
  return useQuery({
    queryKey: copilotKeys.responsibilities,
    queryFn: () => api.get<ResponsibilityDTO[]>("/copilot/responsibilities"),
  });
}

export function useSetResponsibilityState(): UseMutationResult<
  ResponsibilityDTO,
  Error,
  { id: string; state: "active" | "paused" | "done" | "cancelled" }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, state }) =>
      api.patch<ResponsibilityDTO>(`/copilot/responsibilities/${id}`, { state }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: copilotKeys.responsibilities });
      qc.invalidateQueries({ queryKey: copilotKeys.followups });
      qc.invalidateQueries({ queryKey: copilotKeys.profile });
    },
  });
}

export function useFollowups(): UseQueryResult<FollowupDTO[]> {
  return useQuery({
    queryKey: copilotKeys.followups,
    queryFn: () => api.get<FollowupDTO[]>("/copilot/followups"),
  });
}

export function useEndFollowup(): UseMutationResult<void, Error, string> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/copilot/followups/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: copilotKeys.followups });
    },
  });
}

export function useCopilotDelegations(): UseQueryResult<RunDTO[]> {
  return useQuery({
    queryKey: copilotKeys.delegations,
    queryFn: () => api.get<RunDTO[]>("/copilot/delegations"),
  });
}

export function useCopilotNotes(): UseQueryResult<CopilotNoteDTO[]> {
  return useQuery({
    queryKey: copilotKeys.notes,
    queryFn: () => api.get<CopilotNoteDTO[]>("/copilot/notes"),
  });
}

export function useDeleteCopilotNote(): UseMutationResult<void, Error, string> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/copilot/notes/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: copilotKeys.notes });
    },
  });
}
