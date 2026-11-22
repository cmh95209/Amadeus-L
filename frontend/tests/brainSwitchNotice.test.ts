import assert from "node:assert/strict";
import { brainSwitchNotice } from "../src/brainSwitchNotice";

const custom = { mode: "custom", model_missing: false } as const;
const builtin = { mode: "builtin", model_missing: false } as const;
const chatgpt = { mode: "chatgpt", model_missing: false } as const;
const unloaded = "Her built-in model was unloaded (its graphics memory is free).";

assert.equal(brainSwitchNotice("chatgpt", custom), "Switched to your own model.");
assert.equal(brainSwitchNotice("builtin", custom), `Switched to your own model. ${unloaded}`);
assert.equal(brainSwitchNotice("custom", chatgpt), "Switched to ChatGPT.");
assert.equal(brainSwitchNotice("builtin", chatgpt), `Switched to ChatGPT. ${unloaded}`);
assert.equal(brainSwitchNotice("chatgpt", builtin), "Switched to her built-in model - it is loading now.");
assert.equal(brainSwitchNotice("custom", builtin), brainSwitchNotice("chatgpt", builtin));
assert.equal(brainSwitchNotice("chatgpt", { ...builtin, model_missing: true }),
  "Switched to her built-in model. Add a model file (see Model below) and press Load.");

console.log("brainSwitchNotice.test.ts: ok");
