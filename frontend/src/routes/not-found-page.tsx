import { Link } from "@tanstack/react-router";
import { MapPinOff } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";

export function NotFoundPage() {
  return (
    <EmptyState
      icon={MapPinOff}
      title="Page not found"
      description="There is nothing at this address. The link may be out of date."
    >
      <Button asChild className="mt-2 min-h-11">
        <Link to="/scan">Go to Scan</Link>
      </Button>
    </EmptyState>
  );
}
