import { ScanLine } from "lucide-react";

import { EmptyState } from "@/components/empty-state";

export function ScanPage() {
  return (
    <EmptyState
      icon={ScanLine}
      title="Scan a receipt"
      description="Camera capture is on its way. Soon you will photograph a receipt here and Paper Trail will read the store, items and prices for you."
    />
  );
}
