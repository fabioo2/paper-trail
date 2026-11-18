import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  isFinalStatus,
  unwatchReading,
  useReceiptProgress,
  useWatchedReadings,
  type WatchedReading,
} from "@/features/receipts/api";

/** Follows every receipt `watchReading` started, on whichever page the person is, and refetches
 * what it changed once it settles. Renders nothing. */
export function ReadingWatcher() {
  return useWatchedReadings().map((reading) => (
    <Watched key={reading.id} reading={reading} />
  ));
}

function Watched({ reading: { id, until } }: { reading: WatchedReading }) {
  const queryClient = useQueryClient();
  const progress = useReceiptProgress(id, true);
  const settled =
    progress.data !== undefined && isFinalStatus(progress.data.status);
  const failed = progress.isError;

  useEffect(() => {
    if (settled || failed) unwatchReading(queryClient, id, settled);
  }, [settled, failed, id, queryClient]);

  useEffect(() => {
    const timer = setTimeout(() => {
      unwatchReading(queryClient, id, false);
    }, until - Date.now());
    return () => {
      clearTimeout(timer);
    };
  }, [until, id, queryClient]);

  return null;
}
