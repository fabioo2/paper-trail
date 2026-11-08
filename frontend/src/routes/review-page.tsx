import { ClipboardCheck } from "lucide-react";

import { EmptyState } from "@/components/empty-state";

export function ReviewPage() {
  return (
    <EmptyState
      icon={ClipboardCheck}
      title="Nothing to review"
      description="Receipts that need a second look, such as totals that do not add up, wait here for you to check."
    />
  );
}
