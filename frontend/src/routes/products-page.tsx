import { useSearch } from "@tanstack/react-router";

import { ProductList } from "@/features/products/product-list";

export function ProductsPage() {
  const { needsALook = false } = useSearch({ from: "/products" });
  return <ProductList needsALook={needsALook} />;
}
