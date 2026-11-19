import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import UpdateBanner from "./update/UpdateBanner";
import { startErrorMonitoring } from "./monitoring/errors";
import "./styles.css";

startErrorMonitoring(import.meta.env.VITE_SELF_HOSTED_ERRORS_ENABLED === "true");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    <UpdateBanner />
  </StrictMode>,
);
