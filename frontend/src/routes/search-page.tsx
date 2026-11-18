import { useSearch } from "@tanstack/react-router";

import { SearchResults } from "@/features/search/search-results";

export function SearchPage() {
  const { q = "" } = useSearch({ from: "/search" });
  return <SearchResults query={q} />;
}
