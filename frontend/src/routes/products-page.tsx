import { useSearch } from "@tanstack/react-router";

import { ProductList } from "@/features/products/product-list";

export function ProductsPage() {
  const filter = useSearch({ from: "/products" });
  return <ProductList filter={filter} />;
}
