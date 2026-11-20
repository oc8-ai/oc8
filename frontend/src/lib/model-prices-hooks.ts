// Hooks for the global, versioned model-price catalog (Cost Center design,
// 2026-08-19 spec Part A). GLOBAL, not tenant-scoped -- see
// `oc8.api.v1.model_prices`'s module docstring. There is no update endpoint:
// every "edit" is a new version row (POST), and "remove" deactivates the
// current row rather than deleting it (DELETE), matching the append-only
// design described there.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface ModelPrice {
  id: string;
  provider: string;
  modelPattern: string;
  priceInUsdPer1M: number;
  priceOutUsdPer1M: number;
  effectiveFrom: string;
  active: boolean;
}

export interface ModelPriceWriteBody {
  provider: string;
  modelPattern: string;
  priceInUsdPer1M: number;
  priceOutUsdPer1M: number;
}

const keys = {
  modelPrices: ["model-prices"] as const,
};

// Only the current, active row per (provider, modelPattern) -- see
// `list_current_prices` in the backend router. History is a separate
// endpoint this page does not need.
export const useModelPrices = () =>
  useQuery({
    queryKey: keys.modelPrices,
    queryFn: () => api.get<ModelPrice[]>("/model-prices"),
  });

// Whether this deployment lets tenants edit the global prices; a shared
// deployment turns it off (OC8_MODEL_PRICES_READ_ONLY on the backend).
export const useModelPricesPolicy = () =>
  useQuery({
    queryKey: [...keys.modelPrices, "policy"],
    queryFn: () => api.get<{ readOnly: boolean }>("/model-prices/policy"),
  });

export function useCreateModelPrice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ModelPriceWriteBody) => api.post<ModelPrice>("/model-prices", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.modelPrices }),
  });
}

export function useDeactivateModelPrice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/model-prices/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.modelPrices }),
  });
}
