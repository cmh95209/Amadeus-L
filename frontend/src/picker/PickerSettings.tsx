import { useId, useState } from "react";
import { changeOnlineSetting, emojiStyle, saveEmojiStyle, useOnlineSetting } from "./preferences";
import type { EmojiStyle } from "./preferences";

export function PickerSettings() {
  const online = useOnlineSetting();
  const [style, setStyle] = useState(emojiStyle);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const helpId = useId();

  async function toggle(enabled: boolean) {
    setSaving(true);
    setFailed(false);
    try { await changeOnlineSetting(enabled); }
    catch { setFailed(true); }
    finally { setSaving(false); }
  }

  function choose(value: EmojiStyle) {
    saveEmojiStyle(value);
    setStyle(value);
  }

  return (
    <div className="conn-section">
      <label className="picker-setting-switch">
        <input type="checkbox" role="switch" checked={online} disabled={saving}
          aria-describedby={helpId} onChange={(event) => void toggle(event.currentTarget.checked)} />
        <span>Online GIF and sticker search</span>
      </label>
      <p className="settings-help" id={helpId}>Searches are sent to KLIPY and GIPHY.</p>
      {failed && <p className="settings-help" role="alert">Could not save the picker setting.</p>}
      <label>
        Emoji style
        <select value={style} onChange={(event) => choose(event.currentTarget.value as EmojiStyle)}>
          <option value="noto">Noto</option>
          <option value="system">System</option>
        </select>
      </label>
    </div>
  );
}
