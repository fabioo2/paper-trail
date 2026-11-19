import { CloudOff } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { useHealth } from "@/features/health/use-health";

/** Says "Offline" when the backend can't be reached; when connected it is only announced. */
export function ConnectionIndicator() {
  const health = useHealth();

  if (health.isPending) {
    return <Skeleton className="size-2.5 rounded-full" />;
  }

  if (health.isSuccess) {
    return (
      <span role="status" className="sr-only">
        Connected
      </span>
    );
  }
  return (
    <span
      role="status"
      className="flex items-center gap-1 text-xs font-medium text-destructive"
    >
      <CloudOff aria-hidden="true" className="size-4" />
      Offline
    </span>
  );
}
