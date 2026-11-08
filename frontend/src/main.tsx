import "@/styles/globals.css";

import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AppProviders } from "@/app/providers";
import { createQueryClient } from "@/app/query-client";
import { createAppRouter } from "@/app/router";

const queryClient = createQueryClient();
const router = createAppRouter({ queryClient });

const container = document.getElementById("root");
if (!container) throw new Error('index.html has no <div id="root">');

createRoot(container).render(
  <StrictMode>
    <AppProviders queryClient={queryClient}>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
);
