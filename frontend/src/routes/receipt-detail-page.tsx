import { useParams } from "@tanstack/react-router";

import {
  ReceiptDetail,
  ReceiptNotFound,
} from "@/features/receipts/receipt-detail";

export function ReceiptDetailPage() {
  const { receiptId } = useParams({ from: "/receipts/$receiptId" });
  const id = Number(receiptId);
  // Only positive integer ids exist; anything else in the URL is a receipt that is not there.
  return Number.isSafeInteger(id) && id > 0 ? (
    <ReceiptDetail receiptId={id} />
  ) : (
    <ReceiptNotFound />
  );
}
