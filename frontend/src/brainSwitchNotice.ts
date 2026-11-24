import type { BrainMode } from "./api";

export function brainSwitchNotice(previous: BrainMode["mode"], next: Pick<BrainMode, "mode" | "model_missing">): string {
  if (next.mode === "builtin") {
    return next.model_missing
      ? "Switched to her built-in model. Add a model file (see Model below) and press Load."
      : "Switched to her built-in model - it is loading now.";
  }
  if (next.mode === "chatgpt") {
    return previous === "builtin"
      ? "Switched to ChatGPT. Her built-in model was unloaded (its graphics memory is free)."
      : "Switched to ChatGPT.";
  }
  return previous === "builtin"
    ? "Switched to your own model. Her built-in model was unloaded (its graphics memory is free)."
    : "Switched to your own model.";
}
