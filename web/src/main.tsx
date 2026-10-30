import "@fontsource-variable/recursive/mono.css";
import "./theme/tokens.css";
import "./theme/app.css";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: Number.POSITIVE_INFINITY } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
