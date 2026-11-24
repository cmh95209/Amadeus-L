import { useState } from "react";
import { InfoIcon } from "../components/InfoIcon";
import { changeOnlineSetting, emojiStyle, saveEmojiStyle, useOnlineSetting } from "./preferences";
import type { EmojiStyle } from "./preferences";

export function PickerSettings() {
  const online = useOnlineSetting();
  const [style, setStyle] = useState(emojiStyle);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

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
    <div className="settings-section">
      <h4 className="settings-section-title">Emoji, GIFs & stickers</h4>
      <div className="settings-label-line">
        <label className="picker-setting-switch">
          <input type="checkbox" role="switch" checked={online} disabled={saving}
            onChange={(event) => void toggle(event.currentTarget.checked)} />
          <span>Online GIF and sticker search</span>
        </label>
        <InfoIcon>Searches go to KLIPY and GIPHY.</InfoIcon>
      </div>
      {failed && <p className="settings-error" role="alert">Could not save the picker setting.</p>}
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
