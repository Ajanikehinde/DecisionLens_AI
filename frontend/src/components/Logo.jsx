import React, { useState } from "react";
import { Brain } from "lucide-react";

/**
 * Brand mark.
 *
 * Loads the generated assets from `frontend/public/`:
 *   mark.png  -> the DL monogram, used in the sidebar and on the sign-in screen
 *   logo.png  -> full lockup including the "DecisionLens Ai" wordmark (About page)
 *
 * The source artwork had a near-black background which was converted to transparency,
 * so it sits directly on the dark chrome with no plate behind it. If an asset is
 * missing the component degrades to the original Brain glyph rather than showing a
 * broken image.
 */
export default function Logo({
  variant = "mark", // "mark" | "full"
  className = "",
  alt,
}) {
  const [failed, setFailed] = useState(false);
  const src =
    variant === "full"
      ? `${process.env.PUBLIC_URL}/logo.png`
      : `${process.env.PUBLIC_URL}/mark.png`;

  if (failed) {
    return (
      <div
        className={`flex items-center justify-center bg-violet-600/20 border border-violet-500/30 ${className}`}
        role="img"
        aria-label={alt || "DecisionLens AI"}
      >
        <Brain size={18} className="text-violet-300" />
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={alt || "DecisionLens AI"}
      onError={() => setFailed(true)}
      className={`object-contain ${className}`}
      data-testid={`brand-logo-${variant}`}
    />
  );
}