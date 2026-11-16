import { useParams } from "@tanstack/react-router";

import {
  ProductEditor,
  ProductNotFound,
} from "@/features/products/product-editor";

export function ProductDetailPage() {
  const { productId } = useParams({ from: "/products/$productId" });
  const id = Number(productId);
  // Only positive integer ids exist; anything else in the URL is a product that is not there.
  return Number.isSafeInteger(id) && id > 0 ? (
    <ProductEditor productId={id} />
  ) : (
    <ProductNotFound />
  );
}
