import { useEffect, useState, useSyncExternalStore } from "react";
import { getVoiceLanguage, setVoiceLanguage, type VoiceLanguageInfo } from "../api";
import { currentLanguage, subscribe } from "../i18n";
import { SettingsLabel } from "./InfoIcon";

// Settings -> Preferences -> Voice language (2026-11-21): the language her
// VOICE speaks; the text on the screen is unchanged. Saved at once
// (backend /voice_language); her new lines are voiced in it.
//
// Older backends have no /voice_language route: the section stays hidden
// (her voice stays Japanese, exactly as before).
export function VoiceLanguagePicker() {
  const pageLang = useSyncExternalStore(subscribe, currentLanguage);
  const [info, setInfo] = useState<VoiceLanguageInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    getVoiceLanguage()
      .then((data) => {
        if (alive) setInfo(data);
      })
      .catch(() => {
        // Old backend (no route) or the backend is down: hide the setting.
        if (alive) setInfo(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!info) return null;

  async function choose(next: string) {
    setBusy(true);
    setError("");
    try {
      // The reply carries the new state (the note can appear or go away).
      setInfo(await setVoiceLanguage(next));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save that setting");
    } finally {
      setBusy(false);
    }
  }

  // The option names in the page's language (the browser's own language
  // names, so no per-language code). English page: the backend's spelling.
  function optionName(code: string, backendName: string): string {
    if (pageLang === "en") return backendName;
    try {
      const name = new Intl.DisplayNames([pageLang], { type: "language", fallback: "none" }).of(code);
      if (name) return name;
    } catch {
      // Unknown code or no ICU data for it: the backend's name.
    }
    return backendName;
  }

  return (
    <div className="settings-field">
      <SettingsLabel htmlFor="voice-language" tip={info.extra_translation
        ? "Uses an extra translation per reply."
        : "The language she speaks."}>Voice Language</SettingsLabel>
      <select id="voice-language" value={info.code} disabled={busy} onChange={(e) => void choose(e.target.value)}>
        {info.choices.map((c) => (
          // Re-translated on a language change (pageLang re-renders it).
          <option key={c.code} value={c.code} translate="no">
            {optionName(c.code, c.name)}
          </option>
        ))}
      </select>
      {error && <p className="settings-error" role="alert">{error}</p>}
    </div>
  );
}
