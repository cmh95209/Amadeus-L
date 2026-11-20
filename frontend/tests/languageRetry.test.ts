import assert from "node:assert/strict";

// The page loads while the backend is down (2026-11-21): i18n.ts then keeps
// asking /language (first retry 3 s after load, then every 10 s, and on the
// browser "online" event) until the backend CONFIRMS the language - a failed
// fetch no longer looks like a success. App.tsx also calls refreshLanguage()
// the moment its connection probe sees the backend back.
//
// The Node runner has no window/document, and the test must not actually
// wait 3 s, so both are faked: window records its timers and the test fires
// them by hand; fetch is stubbed (backend down, then up).

const g = globalThis as any;
const original = {
  window: g.window,
  document: g.document,
  fetch: g.fetch,
};

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

// The fake window: timers are recorded, not scheduled.
const timers: { delay: number; fn: () => void }[] = [];
const listeners: Record<string, (() => void)[]> = {};
g.window = {
  setTimeout: (fn: () => void, ms: number) => { timers.push({ delay: ms, fn }); return timers.length; },
  clearTimeout: (_id: number) => { /* the test fires timers itself; nothing to cancel */ },
  addEventListener: (name: string, fn: () => void) => { (listeners[name] ||= []).push(fn); },
  removeEventListener: () => { },
  confirm: () => true,
  alert: () => { },
  prompt: () => null,
};
// Just enough document for applyDocument() (no body -> no page walk).
g.document = {
  documentElement: { lang: "en", classList: { toggle: () => { } } },
};

// The backend: down at first, then answers /language with the user's choice.
const languageCalls: string[] = [];
let backendUp = false;
let backendCode = "ja";
g.fetch = async (input: unknown) => {
  const url = String(input);
  if (url.includes("/language")) languageCalls.push(url);
  if (!backendUp) throw new Error("backend down");
  return { ok: true, json: async () => ({ code: backendCode, languages: [] }) };
};

const { initLanguage, refreshLanguage, currentLanguage } = await import("../src/i18n");

try {
  // -- start while the backend is down --------------------------------------
  await initLanguage();
  assert.equal(currentLanguage(), "en", "the page stays English while unconfirmed");
  assert.equal(languageCalls.length, 1, "one /language attempt at load");
  assert.equal(timers.length, 1, "one retry is scheduled");
  assert.equal(timers[0].delay, 3000, "the first retry is 3 s after load");

  // -- the retries keep failing while the backend is down --------------------
  timers[0].fn();   // the 3 s retry
  await tick();
  assert.equal(currentLanguage(), "en", "a failed retry keeps the page English");
  assert.equal(timers.length, 2, "another retry is scheduled");
  assert.equal(timers[1].delay, 10000, "later retries are every 10 s");

  timers[1].fn();   // the 10 s retry
  await tick();
  assert.equal(timers.length, 3, "the retry chain continues");
  assert.equal(timers[2].delay, 10000);

  // -- the backend comes back: App.tsx calls refreshLanguage() ---------------
  backendUp = true;
  assert.equal(await refreshLanguage(), true, "the confirm attempt succeeds");
  assert.equal(currentLanguage(), backendCode, "the confirmed language loads");
  assert.equal(timers.length, 3, "the retry timer is stopped once confirmed");
  assert.equal(languageCalls.length, 4, "no extra attempt for the one in flight");
  assert.equal(await refreshLanguage(), true, "once confirmed it is a no-op");
  assert.equal(languageCalls.length, 4, "a confirmed refreshLanguage makes no request");

  // The "online" event is wired to the same confirm path (fired by the
  // browser when the connection returns).
  assert.equal((listeners.online || []).length, 1, "the online listener is registered");
  assert.equal((listeners.focus || []).length, 1, "the focus re-read is still registered");

  console.log("languageRetry.test.ts: ok");
} finally {
  g.window = original.window;
  g.document = original.document;
  g.fetch = original.fetch;
}
