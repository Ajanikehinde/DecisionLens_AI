import React from "react";
import ReactDOM from "react-dom/client";
import "@/index.css";
import App from "./App";

// QueryClientProvider was removed (DL-021): no page used react-query. Every screen
// fetches with useState + useEffect + axios, so the provider only added a dependency
// and an unused cache. Reintroduce it alongside a real query hook if it is ever needed.
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);