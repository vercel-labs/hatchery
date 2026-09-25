import useSWR from "swr";

import { api, ApiError } from "@/lib/api";

type ApiOptions = {
  keepPreviousData?: boolean;
  waitForCreation?: boolean;
};

// Ported from the agentmesh console. `interval` polls only state that no Hatchery
// stream reports (roster activity of other threads, the live sandbox filesystem).
export function useApi<T>(
  path: string | null,
  interval = 0,
  options: ApiOptions = {},
) {
  return useSWR<T | undefined>(
    path,
    async (key: string) => {
      try {
        return await api<T>(key);
      } catch (error) {
        // An accepted first message can precede the thread's first commit.
        if (
          options.waitForCreation &&
          error instanceof ApiError &&
          error.status === 404
        )
          return undefined;
        throw error;
      }
    },
    {
      refreshInterval: interval,
      dedupingInterval: 1_000,
      errorRetryCount: 2,
      keepPreviousData: options.keepPreviousData ?? false,
    },
  );
}
