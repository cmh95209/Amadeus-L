import { useState, useSyncExternalStore } from "react";
import { availableLanguages, currentLanguage, setLanguage, subscribe } from "../i18n";

// Settings -> Preferences -> Your language (multi-language, 2026-11-19).
// Saved at once (backend/language.txt); the page switches without a reload.
// Her new replies get their translation line in this language; earlier ones
// keep the language they were written in.
export function LanguagePicker() {
  const code = useSyncExternalStore(subscribe, currentLanguage);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const languages = availableLanguages();

  async function choose(next: string) {
    setBusy(true);
    setError("");
    try {
      await setLanguage(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save that setting");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="conn-section">
      <span className="conn-section-title">Your language</span>
      <label>
        Language
        <select value={code} disabled={busy} onChange={(e) => void choose(e.target.value)}
          aria-describedby="your-language-help">
          {languages.map((l) => (
            // Each name in its own script, so anyone can find theirs.
            <option key={l.code} value={l.code} translate="no">
              {l.code === "en" ? l.name : `${l.name} (${l.english})`}
            </option>
          ))}
        </select>
      </label>
      <p className="settings-help" id="your-language-help">
        The page and the translation under her replies. Earlier replies keep their language.
        Saved at once - to switch back by hand, edit backend/language.txt.
      </p>
      {error && <p className="settings-help" role="alert">{error}</p>}
    </div>
  );
}
