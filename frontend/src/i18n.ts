// The page in the user's language (multi-language, 2026-11-19).
//
// How it works - and why components need no translation code:
// the page is still written in English. This module watches the page and
// swaps every text node / title / placeholder / aria-label / alt whose
// English matches a line of the user's language file for its translation
// (exact lines, or lines with {1} {2} slots for numbers and names). React
// keeps rendering English; a node it changes is simply translated again.
// Chat text (her replies, your messages - LinkedText) is marked
// translate="no" and is never touched.
//
// A new feature: write its English as usual, run
// `node scripts/lang_extract.mjs` (lists the new lines in lang/en.json)
// and `python scripts/lang_fill.py` (adds them to the other languages).
// Until then the new lines show in English - nothing breaks.
//
// The choice lives in backend/language.txt (GET/POST /language); the page
// re-reads it when the window gets focus, so a hand edit applies on the
// next click into the page.
import { API_BASE } from "./api";

export type LanguageInfo = { code: string; name: string; english: string; rtl: boolean };
type LangFile = { page?: Record<string, string>; codes?: Record<string, string> };

// One loader per language: Vite splits each into its own small file, so
// only the chosen language is downloaded.
const LOADERS: Record<string, () => Promise<{ default: LangFile }>> = {
  zh: () => import("./lang/zh.json"),
  ja: () => import("./lang/ja.json"),
  ko: () => import("./lang/ko.json"),
  es: () => import("./lang/es.json"),
  fr: () => import("./lang/fr.json"),
  de: () => import("./lang/de.json"),
  pt: () => import("./lang/pt.json"),
  ru: () => import("./lang/ru.json"),
  ar: () => import("./lang/ar.json"),
  hi: () => import("./lang/hi.json"),
  ta: () => import("./lang/ta.json"),
  ms: () => import("./lang/ms.json"),
  id: () => import("./lang/id.json"),
  th: () => import("./lang/th.json"),
  vi: () => import("./lang/vi.json"),
};

// --- TUNABLES -------------------------------------------------------------
const ATTRS = ["title", "placeholder", "aria-label", "alt"];
const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE", "PRE", "NOSCRIPT"]);
// Their text is never touched, but their placeholder / title are.
const ATTRS_ONLY = new Set(["TEXTAREA"]);
const FALLBACK_LANGUAGES: LanguageInfo[] = [{ code: "en", name: "English", english: "English", rtl: false }];
// The backend was unreachable when the page loaded (the language is not
// confirmed yet): first re-ask 3 s after load, then every 10 s - and the
// moment the browser reports it is back online.
const LANGUAGE_RETRY_FIRST_MS = 3000;
const LANGUAGE_RETRY_EVERY_MS = 10000;
// ---------------------------------------------------------------------------

let code = "en";
let languages: LanguageInfo[] = FALLBACK_LANGUAGES;
let exact = new Map<string, string>();
let patterns: { re: RegExp; slots: string[]; out: string }[] = [];
let codes: Record<string, string> = {};
const listeners = new Set<() => void>();

// What we changed, so a switch back to English (or another language) can
// restore the English first.
const textOrig = new WeakMap<Text, { orig: string; shown: string }>();
const attrOrig = new WeakMap<Element, Map<string, { orig: string; shown: string }>>();

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function build(file: LangFile | null) {
  exact = new Map();
  patterns = [];
  codes = file?.codes ?? {};
  for (const [en, tr] of Object.entries(file?.page ?? {})) {
    if (typeof tr !== "string" || !tr.trim() || tr === en) continue;
    if (/\{\d+\}/.test(en)) {
      const slots: string[] = [];
      const re = new RegExp("^" + en.split(/(\{\d+\})/).map((part) => {
        const m = part.match(/^\{(\d+)\}$/);
        if (m) { slots.push(m[1]); return "([\\s\\S]+?)"; }
        return escapeRe(part);
      }).join("") + "$");
      patterns.push({ re, slots, out: tr });
    } else {
      exact.set(en, tr);
    }
  }
  // Longer patterns first: the most specific line wins.
  patterns.sort((a, b) => b.re.source.length - a.re.source.length);
}

/** One English line -> the user's language (the line itself when unknown). */
export function t(english: string, vars?: Record<string, string | number>): string {
  const key = english.replace(/\s+/g, " ").trim();
  let out = translateLine(key) ?? english;
  if (vars) for (const [k, v] of Object.entries(vars)) out = out.split(`{${k}}`).join(String(v));
  return out;
}

function translateLine(key: string): string | null {
  if (code === "en" || !key) return null;
  const hit = exact.get(key);
  if (hit !== undefined) return hit;
  if (!/\p{L}/u.test(key) || key.length > 600) return null;
  for (const p of patterns) {
    const m = key.match(p.re);
    if (!m) continue;
    let out = p.out;
    p.slots.forEach((slot, i) => {
      // A slot that is itself a known line ("· read {1}") is translated too.
      const value = m[i + 1];
      out = out.split(`{${slot}}`).join(exact.get(value.trim()) ?? value);
    });
    return out;
  }
  return null;
}

function translateText(english: string): string | null {
  const key = english.replace(/\s+/g, " ").trim();
  const tr = translateLine(key);
  if (tr === null) return null;
  // keep the spaces around it (JSX text next to other elements)
  const lead = english.match(/^\s*/)![0];
  const trail = english.match(/\s*$/)![0];
  return lead + tr + trail;
}

function skipped(el: Element | null): boolean {
  for (let e = el; e; e = e.parentElement) {
    if (SKIP_TAGS.has(e.tagName)) return true;
    if (e.getAttribute("translate") === "no" || (e as HTMLElement).isContentEditable) return true;
  }
  return false;
}

function doText(node: Text) {
  const known = textOrig.get(node);
  // React wrote new English since we translated it: that is the new original.
  const orig = known && node.data === known.shown ? known.orig : node.data;
  const tr = translateText(orig);
  if (tr === null || tr === orig) {
    if (known && node.data === known.shown) node.data = known.orig;
    textOrig.delete(node);
    return;
  }
  if (node.data !== tr) node.data = tr;
  textOrig.set(node, { orig, shown: tr });
}

function doAttrs(el: Element) {
  let saved = attrOrig.get(el);
  for (const name of ATTRS) {
    const value = el.getAttribute(name);
    if (value === null) continue;
    const known = saved?.get(name);
    const orig = known && value === known.shown ? known.orig : value;
    const tr = translateText(orig);
    if (tr === null || tr === orig) {
      if (known && value === known.shown) el.setAttribute(name, known.orig);
      saved?.delete(name);
      continue;
    }
    if (value !== tr) el.setAttribute(name, tr);
    if (!saved) { saved = new Map(); attrOrig.set(el, saved); }
    saved.set(name, { orig, shown: tr });
  }
}

function walk(root: Node) {
  if (root.nodeType === Node.TEXT_NODE) {
    if (!skipped(root.parentElement)) doText(root as Text);
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  const el = root as Element;
  if (skipped(el)) return;
  doAttrs(el);
  const tw = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      if (n.nodeType === Node.ELEMENT_NODE) {
        const e = n as Element;
        if (ATTRS_ONLY.has(e.tagName) && e.getAttribute("translate") !== "no") doAttrs(e);
        if (SKIP_TAGS.has(e.tagName) || e.getAttribute("translate") === "no" ||
            (e as HTMLElement).isContentEditable) return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let n = tw.nextNode(); n; n = tw.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) doText(n as Text);
    else doAttrs(n as Element);
  }
}

let observer: MutationObserver | null = null;
let busy = false;

function startObserver() {
  if (observer || typeof MutationObserver === "undefined") return;
  observer = new MutationObserver((records) => {
    if (busy) return;
    busy = true;
    try {
      for (const r of records) {
        if (r.type === "characterData") {
          const n = r.target as Text;
          const known = textOrig.get(n);
          if (known && n.data === known.shown) continue;   // our own change
          if (!skipped(n.parentElement)) doText(n);
        } else if (r.type === "attributes") {
          const el = r.target as Element;
          if (ATTRS_ONLY.has(el.tagName) ? !skipped(el.parentElement) : !skipped(el)) doAttrs(el);
        } else {
          r.addedNodes.forEach((n) => walk(n));
        }
      }
    } finally {
      busy = false;
    }
  });
  observer.observe(document.body, {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ATTRS,
  });
}

// window.confirm / alert / prompt show English text the page passes in.
let dialogsPatched = false;
function patchDialogs() {
  if (dialogsPatched || typeof window === "undefined") return;
  dialogsPatched = true;
  const { confirm, alert, prompt } = window;
  window.confirm = (msg?: string) => confirm.call(window, msg === undefined ? msg : translateMultiline(String(msg)));
  window.alert = (msg?: unknown) => alert.call(window, msg === undefined ? msg : translateMultiline(String(msg)));
  window.prompt = (msg?: string, def?: string) =>
    prompt.call(window, msg === undefined ? msg : translateMultiline(String(msg)), def);
}

function translateMultiline(s: string): string {
  const whole = translateLine(s.replace(/\s+/g, " ").trim());
  if (whole !== null) return whole;
  return s.split("\n").map((line) => translateText(line) ?? line).join("\n");
}

function applyDocument() {
  const info = languages.find((l) => l.code === code);
  // The layout stays as it is (her character, the panels); right-to-left
  // text (Arabic) flows right to left inside each line (styles.css, .rtl).
  document.documentElement.lang = code;
  document.documentElement.classList.toggle("rtl", !!info?.rtl);
  if (typeof document === "undefined" || !document.body) return;
  busy = true;
  try { walk(document.body); } finally { busy = false; }
  if (document.title) {
    const tr = translateLine(document.title);
    if (tr) document.title = tr;
  }
}

async function load(next: string) {
  let file: LangFile | null = null;
  if (next !== "en" && LOADERS[next]) {
    try { file = (await LOADERS[next]()).default; } catch { file = null; }
  }
  code = file || next === "en" ? next : "en";
  build(file);
  applyDocument();
  listeners.forEach((fn) => fn());
}

// null = the backend did not answer: the page keeps its current language.
async function fetchChoice(): Promise<string | null> {
  try {
    const res = await fetch(`${API_BASE}/language`);
    if (!res.ok) return null;
    const data = await res.json();
    if (Array.isArray(data.languages) && data.languages.length) languages = data.languages;
    return typeof data.code === "string" ? data.code : null;
  } catch {
    return null;
  }
}

// Whether the backend has confirmed the user's language. Until it does (the
// page loaded while the backend was down), the page stays on its current
// language and keeps asking: a timer, the "online" event, and the app's
// refreshLanguage() when it sees the backend back.
let confirmed = false;
let retryInFlight = false;
let retryTimer: number | null = null;

function stopRetry() {
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function armRetry(first: boolean) {
  if (confirmed) return;
  stopRetry();
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    void confirmLanguage().then((ok) => { if (!ok) armRetry(false); });
  }, first ? LANGUAGE_RETRY_FIRST_MS : LANGUAGE_RETRY_EVERY_MS);
}

/** One confirm attempt; on success loads the language (only if it differs). */
async function confirmLanguage(): Promise<boolean> {
  if (confirmed || retryInFlight) return confirmed;
  retryInFlight = true;
  try {
    const next = await fetchChoice();
    if (next === null) return false;
    confirmed = true;
    stopRetry();
    if (next !== code) await load(next);
    return true;
  } finally {
    retryInFlight = false;
  }
}

/** One confirm attempt (the app calls it when the backend comes back). */
export function refreshLanguage(): Promise<boolean> {
  if (confirmed) return Promise.resolve(true);
  stopRetry();
  return confirmLanguage().then((ok) => { if (!ok) armRetry(false); return ok; });
}

/** At start: read the user's language, translate the page, keep watching. */
export async function initLanguage(): Promise<void> {
  patchDialogs();
  startObserver();
  const next = await fetchChoice();
  if (next === null) {
    // The backend is down: stay put (English) and keep asking until it
    // confirms the language (timer / "online" / refreshLanguage()).
    await load(code);
    armRetry(true);
  } else {
    confirmed = true;
    if (next !== code) await load(next);
  }
  // A hand edit of backend/language.txt applies when the page gets focus.
  window.addEventListener("focus", () => {
    void fetchChoice().then((choice) => { if (choice !== null && choice !== code) void load(choice); });
  });
  window.addEventListener("online", () => { if (!confirmed) void refreshLanguage(); });
}

/** Settings -> Preferences -> Your language. */
export async function setLanguage(next: string): Promise<void> {
  const res = await fetch(`${API_BASE}/language`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: next }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.message || `Request failed (${res.status})`);
  if (Array.isArray(data.languages)) languages = data.languages;
  await load(next);
}

export function currentLanguage(): string { return code; }
export function availableLanguages(): LanguageInfo[] { return languages; }
/** The browser locale for dates and numbers ("zh", "ta" ...). */
export function locale(): string { return code; }

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

// --- Messages the backend sends with a code (reply_errors.Coded) ----------
// {"message": English, "message_key": "error.status", "message_vars": {...}}
// The language file's "codes" section holds the sentences; "error.status"
// is built from its parts. Unknown code or no translation: the English.
export function codedMessage(data: { message?: string; message_key?: string;
                                     message_vars?: Record<string, unknown> } | null | undefined,
                             fallback: string): string {
  const english = data?.message || fallback;
  const key = data?.message_key;
  if (!key || code === "en") return english;
  const vars = Object.fromEntries(Object.entries(data?.message_vars ?? {})
    .map(([k, v]) => [k, v == null ? "" : String(v)]));
  const fill = (s: string) => s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
  let text: string | undefined;
  if (key === "error.status") {
    const what = codes[`error.what.${vars.kind}`];
    const label = codes[`error.label.${vars.kind}`];
    const frame = codes["error.status"];
    if (!what || !label || !frame) return english;
    vars.model = vars.model ? `"${vars.model}"` : (codes["error.this_model"] ?? "this model");
    vars.label = label;
    vars.what = fill(what);
    vars.said = vars.reason ? fill(codes["error.said"] ?? ' It says: "{reason}".') : "";
    text = fill(frame);
  } else if (codes[key]) {
    text = fill(codes[key]);
  }
  if (!text) return english;
  if (vars.turn && codes[`error.turn.${vars.turn}`]) text = codes[`error.turn.${vars.turn}`] + text;
  return text;
}
