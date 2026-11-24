import { useState, useSyncExternalStore } from "react";
import { availableLanguages, currentLanguage, setLanguage, subscribe } from "../i18n";
import { SettingsLabel } from "./InfoIcon";

// Settings -> Preferences -> Display Language.
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
    <div className="settings-field">
      <SettingsLabel htmlFor="display-language" tip="Interface and reply translations.">Display Language</SettingsLabel>
      <select id="display-language" value={code} disabled={busy} onChange={(e) => void choose(e.target.value)}>
        {languages.map((l) => (
          // Each name in its own script, so anyone can find theirs.
          <option key={l.code} value={l.code} translate="no">
            {l.code === "en" ? l.name : `${l.name} (${l.english})`}
          </option>
        ))}
      </select>
      {error && <p className="settings-error" role="alert">{error}</p>}
    </div>
  );
}
