import { useId, useState } from "react";
import type { ReactNode } from "react";

export function InfoIcon({ children }: { children: ReactNode }) {
  const id = useId();
  const [pinned, setPinned] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  return (
    <span className="info-tip" data-pinned={pinned} data-dismissed={dismissed}
      onPointerEnter={() => setDismissed(false)}>
      <button
        type="button"
        className="info-tip-button"
        aria-label="More information"
        aria-describedby={id}
        onClick={(event) => {
          event.currentTarget.focus();
          setPinned(!pinned);
          setDismissed(pinned);
        }}
        onFocus={() => setDismissed(false)}
        onBlur={() => { setPinned(false); setDismissed(false); }}
        onKeyDown={(event) => {
          if (event.key !== "Escape" || dismissed) return;
          event.stopPropagation();
          setPinned(false);
          setDismissed(true);
        }}
      >
        <span aria-hidden="true">i</span>
      </button>
      <span className="info-tip-text" id={id} role="tooltip">{children}</span>
    </span>
  );
}

export function SettingsLabel({ htmlFor, tip, children }: {
  htmlFor: string;
  tip: ReactNode;
  children: ReactNode;
}) {
  return (
    <span className="settings-label-line">
      <label htmlFor={htmlFor}>{children}</label>
      <InfoIcon>{tip}</InfoIcon>
    </span>
  );
}
