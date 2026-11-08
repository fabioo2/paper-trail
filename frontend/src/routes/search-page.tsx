import { Search } from "lucide-react";

import { EmptyState } from "@/components/empty-state";

export function SearchPage() {
  return (
    <EmptyState
      icon={Search}
      title="Search your purchases"
      description="Find anything you have bought by name, store or category, even when the receipt abbreviates it."
    />
  );
}
