import { useEffect, useState } from "react";
import { getVoiceLanguage, setVoiceLanguage, type VoiceLanguageInfo } from "../api";

// Settings -> Preferences -> Voice language (2026-11-21): the language her
// VOICE speaks; the text on the screen is unchanged. Saved at once
// (backend /voice_language); her new lines are voiced in it.
//
// Older backends have no /voice_language route: the section stays hidden
// (her voice stays Japanese, exactly as before).
export function VoiceLanguagePicker() {
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

  return (
    <div className="conn-section">
      <span className="conn-section-title">Voice language</span>
      <label>
        Language
        <select value={info.code} disabled={busy} onChange={(e) => void choose(e.target.value)}
          aria-describedby="voice-language-help">
          {info.choices.map((c) => (
            // The names as the backend spells them.
            <option key={c.code} value={c.code} translate="no">
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <p className="settings-help" id="voice-language-help">
        The language her voice speaks. The text on the screen is unchanged.
      </p>
      {info.extra_translation && (
        <p className="settings-help">Uses an extra translation per reply.</p>
      )}
      {error && <p className="settings-help" role="alert">{error}</p>}
    </div>
  );
}
