// Collect the English text the page shows (multi-language, 2026-11-19).
//
//   node scripts/lang_extract.mjs          -> writes src/lang/en.json "page"
//   node scripts/lang_extract.mjs --check  -> lists lines missing per language
//
// The page translates on screen (src/i18n.ts): any text node / title /
// placeholder / aria-label whose English matches a key of the user's
// language file is swapped for its translation. This script finds those
// English texts in src/ (JSX text, those attributes, and sentence-like
// strings in code, e.g. status messages) so the language files can list
// them. A template literal `Saved ${n} files` becomes the key
// "Saved {1} files". A new feature therefore needs no translation code at
// all: run this script, then fill the new lines (scripts/lang_fill.py).
import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SRC = join(ROOT, "src");
const LANG = join(SRC, "lang");

const SKIP_DIRS = new Set(["lang", "vendor", "live2d", "cubism"]);
const SKIP_FILES = new Set(["i18n.ts"]);   // the translator itself
const TEXT_ATTRS = new Set(["title", "placeholder", "aria-label", "alt", "label", "data-tip"]);
const NON_TEXT_ATTRS = new Set(["className", "class", "key", "href", "src", "id", "type", "name",
  "role", "lang", "rel", "target", "htmlFor", "autoComplete", "accept", "inputMode", "method",
  "translate", "dir", "style", "value", "defaultValue", "pattern", "download", "form"]);
const SKIP_CALLS = new Set(["log", "warn", "error", "debug", "info", "fetch", "querySelector",
  "querySelectorAll", "getElementById", "addEventListener", "removeEventListener",
  "setAttribute", "getAttribute", "removeAttribute", "postMessage", "getItem", "setItem",
  "removeItem", "startsWith", "endsWith", "includes", "split", "replace", "match", "test",
  "indexOf", "join", "matchMedia", "createElement", "dispatchEvent", "has", "get", "set",
  "delete", "require", "import", "toLocaleString", "toLocaleDateString", "toLocaleTimeString",
  "Intl", "RegExp", "Event", "CustomEvent", "localStorage", "sessionStorage", "JSON"]);

function files(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (!SKIP_DIRS.has(name)) out.push(...files(p));
    } else if (/\.(tsx|ts)$/.test(name) && !name.endsWith(".d.ts") && !SKIP_FILES.has(name)) out.push(p);
  }
  return out;
}

const letters = (s) => (s.match(/\p{L}/gu) || []).length;

// Never translated: names, commands, file paths, css/animation names.
const IGNORE = new Set(["Alex", "Amadeus", "AMADEUS", "CharacterMemory", "GPT-SoVITS", "en-US",
  "PatReaction", "TapReaction", "personality-help personality-hint", "tailscale funnel reset",
  "backend/data", "Pasted text.txt", "Mac", "JSON key", "Top K", "Top P", "Min P"]);

// Text a user reads: at least two letters, not code-looking.
function looksLikeText(s, strict) {
  const t = s.trim();
  if (letters(t) < 2) return false;
  if (IGNORE.has(t) || /^M[\d{]/.test(t)) return false;            // svg paths
  if (/^(https?:|\/|\.\/|#|data:|blob:|mailto:)/.test(t)) return false;
  if (/^[a-z0-9_.-]+$/.test(t) && strict) return false;          // ids, keys, css names
  if (/^[a-z]+([A-Z][a-z]*)+$/.test(t)) return false;              // camelCase
  if (/[{}<>;=]|=>|\$\(/.test(t) && !/\{\d+\}/.test(t)) return false;
  if (/^[\w-]+\.(png|jpg|wav|mp3|json|txt|gguf|webp|svg|css|js)$/i.test(t)) return false;
  if (/^(GET|POST|PUT|DELETE|PATCH)$/.test(t)) return false;
  if (strict) {
    // A string in plain code: a sentence or a label (starts upper-case or
    // has a space), never a lower-case token.
    if (!/^[\p{Lu}"“(…]/u.test(t) && !/\s/.test(t)) return false;
    if (!/\s/.test(t) && t.length > 24) return false;
    if (/^[A-Z_]+$/.test(t)) return false;                         // CONSTANTS
  }
  return true;
}

function templateKey(node, sf) {
  // `Saved ${n} files` -> "Saved {1} files"
  let out = node.head.text;
  node.templateSpans.forEach((span, i) => { out += `{${i + 1}}` + span.literal.text; });
  return out;
}

function attrName(node) {
  let p = node.parent;
  while (p && !ts.isJsxAttribute(p)) {
    if (ts.isJsxElement(p) || ts.isJsxSelfClosingElement(p) || ts.isBlock(p) || ts.isSourceFile(p)) return null;
    p = p.parent;
  }
  return p ? p.name.getText() : null;
}

function insideSkippedCall(node) {
  let p = node.parent;
  for (let depth = 0; p && depth < 6; depth++, p = p.parent) {
    if (ts.isCallExpression(p) || ts.isNewExpression(p)) {
      const e = p.expression;
      const name = ts.isPropertyAccessExpression(e) ? e.name.text : ts.isIdentifier(e) ? e.text : "";
      const obj = ts.isPropertyAccessExpression(e) && ts.isIdentifier(e.expression) ? e.expression.text : "";
      if (SKIP_CALLS.has(name) || SKIP_CALLS.has(obj) || obj === "console") return true;
      return false;
    }
    if (ts.isImportDeclaration(p) || ts.isExportDeclaration(p)) return true;
    if (ts.isElementAccessExpression(p)) return true;
    if (ts.isCaseClause(p)) return true;
    if (ts.isBinaryExpression(p) && [ts.SyntaxKind.EqualsEqualsEqualsToken,
      ts.SyntaxKind.ExclamationEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsToken,
      ts.SyntaxKind.ExclamationEqualsToken].includes(p.operatorToken.kind)) return true;
    if (ts.isPropertyAssignment(p) && p.name && ts.isIdentifier(p.name) &&
        NON_TEXT_ATTRS.has(p.name.text)) return true;
    if (ts.isLiteralTypeNode(p) || ts.isUnionTypeNode(p) || ts.isTypeReferenceNode(p)) return true;
  }
  return false;
}

export function collect() {
  const found = new Map();   // key -> first place
  const add = (s, where) => {
    const k = s.replace(/\s+/g, " ").trim();
    if (k && !found.has(k)) found.set(k, where);
  };
  for (const file of files(SRC)) {
    const text = readFileSync(file, "utf8");
    const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true,
      file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const rel = relative(ROOT, file).replace(/\\/g, "/");
    const where = (n) => `${rel}:${sf.getLineAndCharacterOfPosition(n.getStart()).line + 1}`;
    const visit = (node) => {
      if (ts.isJsxText(node)) {
        if (looksLikeText(node.text, false)) add(node.text, where(node));
      } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) ||
                 ts.isTemplateExpression(node)) {
        const value = ts.isTemplateExpression(node) ? templateKey(node, sf) : node.text;
        const attr = attrName(node);
        if (attr) {
          if (TEXT_ATTRS.has(attr) && looksLikeText(value, false)) add(value, where(node));
          else if (!NON_TEXT_ATTRS.has(attr) && !/^on[A-Z]/.test(attr) &&
                   looksLikeText(value, true) && !insideSkippedCall(node)) add(value, where(node));
        } else if (!insideSkippedCall(node) && looksLikeText(value, true)) {
          add(value, where(node));
        }
        if (ts.isTemplateExpression(node)) {
          // also the plain pieces between ${...} (they can end up as their
          // own text nodes in JSX)
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }
  return found;
}

function readJson(p) {
  try { return JSON.parse(readFileSync(p, "utf8")); } catch { return {}; }
}

const found = collect();
if (process.argv.includes("--check")) {
  const keys = [...found.keys()];
  for (const name of readdirSync(LANG).filter((n) => n.endsWith(".json") && n !== "en.json")) {
    const data = readJson(join(LANG, name));
    const page = data.page || {};
    const missing = keys.filter((k) => !(k in page));
    console.log(`${name}: ${missing.length} of ${keys.length} page lines missing`);
  }
} else {
  const path = join(LANG, "en.json");
  const old = existsSync(path) ? readJson(path) : {};
  const page = {};
  for (const k of [...found.keys()].sort((a, b) => a.localeCompare(b))) page[k] = k;
  const out = { ...old, page };
  writeFileSync(path, JSON.stringify(out, null, 1) + "\n", "utf8");
  console.log(`en.json: ${Object.keys(page).length} page lines`);
}
