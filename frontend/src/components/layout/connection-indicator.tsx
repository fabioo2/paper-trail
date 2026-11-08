import { Skeleton } from "@/components/ui/skeleton";
import { useHealth } from "@/features/health/use-health";
import { cn } from "@/lib/utils";

/** A status dot for the backend connection; the wording is for screen readers. */
export function ConnectionIndicator() {
  const health = useHealth();

  if (health.isPending) {
    return <Skeleton className="size-2.5 rounded-full" />;
  }

  const online = health.isSuccess;
  return (
    <span role="status" className="flex size-6 items-center justify-center">
      <span
        aria-hidden="true"
        className={cn(
          "size-2.5 rounded-full",
          online ? "bg-success" : "bg-destructive",
        )}
      />
      <span className="sr-only">{online ? "Connected" : "Offline"}</span>
    </span>
  );
}
