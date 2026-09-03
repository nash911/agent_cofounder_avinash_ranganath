import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.js";
import { appConfig } from "./app-config.js";
import { ErrorBoundary } from "./components/ErrorBoundary.js";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Missing application root");

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary storageKey={appConfig.storageKey}>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
