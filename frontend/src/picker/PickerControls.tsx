import { useLayoutEffect, useRef } from "react";
import type { ReactNode } from "react";
import { absoluteUrl } from "../attachments";

export function CategoryButton({ label, active, icon, image, onClick }: {
  label: string; active?: boolean; icon: string; image?: string | null; onClick: () => void;
}) {
  return (
    <button type="button" className="picker-category" aria-pressed={active} aria-label={label}
      title={label} onClick={onClick}>
      {image ? <img src={absoluteUrl(image)} alt="" loading="lazy" decoding="async" />
        : <span aria-hidden="true" className="picker-emoji-font">{icon}</span>}
    </button>
  );
}

export function PickerSearch({ label, value, onChange, maxLength }: {
  label: string; value: string; onChange: (value: string) => void; maxLength?: number;
}) {
  return <input className="picker-search" type="search" aria-label={label} placeholder={label}
    maxLength={maxLength} value={value} onChange={(event) => onChange(event.currentTarget.value)} />;
}

export function PickerMenu({ label, children, close }: { label: string; children: ReactNode; close: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { root.current?.querySelector<HTMLElement>("button, input")?.focus({ preventScroll: true }); }, []);
  return (
    <div className="picker-menu-backdrop" onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
      <div className="picker-menu" ref={root} role="group" aria-label={label}
        onKeyDown={(event) => {
          if (event.key === "Escape") { event.stopPropagation(); close(); }
        }}>
        <div className="picker-menu-heading">
          <strong>{label}</strong>
          <button type="button" aria-label="Close" onClick={close}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
}
