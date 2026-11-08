import { Badge } from "@/components/ui/badge";
import type { ReceiptStatus } from "@/features/receipts/api";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

const PRESENTATION: Record<
  ReceiptStatus,
  { label: string; variant: BadgeVariant; className?: string }
> = {
  processing: { label: "Processing", variant: "secondary" },
  needs_review: {
    label: "Needs review",
    variant: "outline",
    className: "border-warning",
  },
  completed: { label: "Completed", variant: "default" },
  failed: { label: "Failed", variant: "destructive" },
};

/** The receipt's status as a coloured label, e.g. "Needs review". */
export function ReceiptStatusBadge({ status }: { status: ReceiptStatus }) {
  const { label, variant, className } = PRESENTATION[status];
  return (
    <Badge variant={variant} className={className}>
      {label}
    </Badge>
  );
}
