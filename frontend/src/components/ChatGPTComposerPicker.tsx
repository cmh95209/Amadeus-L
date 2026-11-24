import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { currentLanguage, subscribe, t } from "../i18n";
import { CHATGPT_REASONING_LABELS } from "./ChatGPTConnection";
import type { useChatGPTConnection } from "./ChatGPTConnection";

export default function ChatGPTComposerPicker({ connection, disabled }: {
  connection: ReturnType<typeof useChatGPTConnection>;
  disabled: boolean;
}) {
  const { status, models, busy, chooseModel, reasoning } = connection;
  const [open, setOpen] = useState(false);
  const picker = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const currentOption = useRef<HTMLButtonElement>(null);
  const dialogId = useId();
  const stopsId = useId();
  useSyncExternalStore(subscribe, currentLanguage);

  const model = status?.model ?? "";
  const name = models.find((option) => option.id === model)?.name ?? model;
  const levelLabel = CHATGPT_REASONING_LABELS[reasoning.level];
  const unavailable = disabled || !status?.signed_in || models.length === 0;

  useEffect(() => {
    if (!open) return;
    if (unavailable) {
      setOpen(false);
      return;
    }
    currentOption.current?.focus();
    function closeOutside(event: PointerEvent) {
      if (event.target instanceof Node && !picker.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open, unavailable]);

  return (
    <div className="chatgpt-composer-picker" ref={picker}
      onBlur={(event) => {
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (open && event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus();
        }
      }}>
      <button type="button" className="chatgpt-picker-trigger" ref={trigger}
        aria-haspopup="dialog" aria-expanded={open && !unavailable} aria-controls={open && !unavailable ? dialogId : undefined}
        disabled={unavailable} onClick={() => setOpen(!open)}>
        {name ? <span className="chatgpt-picker-name" translate="no">{name}</span> : <span>Choose a model</span>}
        <span> · {levelLabel}</span>
        <span className="chatgpt-picker-chevron" aria-hidden="true" />
      </button>
      {open && !unavailable && (
        <div id={dialogId} className="chatgpt-picker-popover" role="dialog" aria-label="ChatGPT model and reasoning">
          <ul className="chatgpt-model-options">
            {models.map((option) => (
              <li key={option.id}>
                <button type="button" ref={option.id === model ? currentOption : undefined}
                  aria-pressed={option.id === model} aria-disabled={busy}
                  onClick={() => { if (!busy && option.id !== model) chooseModel(option.id); }}>
                  <span translate="no">{option.name}</span>
                  {option.id === model && <span aria-hidden="true">✓</span>}
                </button>
              </li>
            ))}
          </ul>
          <div className="chatgpt-picker-reasoning">
            {reasoning.levels.length ? (
              <label>
                <span><span translate="no">{name}</span>{" "}{levelLabel}</span>
                <input type="range" min={0} max={reasoning.levels.length - 1} step={1}
                  value={Math.max(0, reasoning.levels.indexOf(reasoning.level))}
                  list={stopsId} aria-label="Reasoning" aria-valuetext={t(levelLabel)}
                  disabled={reasoning.disabled || reasoning.levels.length === 1}
                  onChange={(event) => reasoning.choose(reasoning.levels[event.target.valueAsNumber])} />
                <datalist id={stopsId}>
                  {reasoning.levels.map((level, index) => <option key={level} value={index} />)}
                </datalist>
              </label>
            ) : <span>Reasoning unavailable</span>}
          </div>
        </div>
      )}
    </div>
  );
}
