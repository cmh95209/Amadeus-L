import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import { LONG_PRESS_MS, LONG_PRESS_SLOP, SEARCH_DELAY_MS } from "./tunables";

export function useDebouncedQuery(query: string) {
  const [value, setValue] = useState(query);
  useEffect(() => {
    if (!query) {
      setValue("");
      return;
    }
    const timer = window.setTimeout(() => setValue(query), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [query]);
  return query ? value : "";
}

export function useLongPress() {
  const timer = useRef<number | undefined>(undefined);
  const start = useRef({ x: 0, y: 0 });
  const swallowed = useRef<HTMLElement | null>(null);
  function cancel() { window.clearTimeout(timer.current); }
  useEffect(() => cancel, []);

  function bind(open: (button: HTMLElement) => void) {
    return {
      onPointerDown: (event: PointerEvent<HTMLButtonElement>) => {
        cancel();
        swallowed.current = null;
        if (event.pointerType === "mouse" || !event.isPrimary) return;
        const button = event.currentTarget;
        start.current = { x: event.clientX, y: event.clientY };
        timer.current = window.setTimeout(() => {
          swallowed.current = button;
          open(button);
        }, LONG_PRESS_MS);
      },
      onPointerMove: (event: PointerEvent<HTMLButtonElement>) => {
        if (Math.hypot(event.clientX - start.current.x, event.clientY - start.current.y) > LONG_PRESS_SLOP) cancel();
      },
      onPointerUp: cancel, onPointerCancel: cancel, onPointerLeave: cancel,
      onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => {
        if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) {
          event.preventDefault();
          open(event.currentTarget);
        }
      },
      onContextMenu: (event: React.MouseEvent<HTMLButtonElement>) => {
        event.preventDefault();
        cancel();
        if (swallowed.current !== event.currentTarget) {
          swallowed.current = event.currentTarget;
          open(event.currentTarget);
        }
      },
    };
  }

  function consume(button: HTMLElement, detail: number) {
    if (detail === 0 || swallowed.current !== button) return false;
    swallowed.current = null;
    return true;
  }
  return { bind, consume, cancel };
}

const GRID_CELL = "button[data-picker-cell]";

function tabStop(button: HTMLButtonElement | null, index: number) {
  if (!button) return;
  button.tabIndex = index;
  const action = button.nextElementSibling;
  if (action instanceof HTMLButtonElement && action.hasAttribute("data-picker-action")) action.tabIndex = index;
}

/** Keep a single primary tab stop and let arrows cross category sections. */
export function useGridKeys() {
  const active = useRef<HTMLButtonElement | null>(null);
  return {
    onFocus: (event: React.FocusEvent<HTMLElement>) => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>(GRID_CELL);
      if (!button) return;
      const first = event.currentTarget.querySelector<HTMLButtonElement>(GRID_CELL);
      tabStop(first, -1);
      tabStop(active.current, -1);
      tabStop(button, 0);
      active.current = button;
    },
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      const targetButton = (event.target as HTMLElement).closest("button");
      const cell = targetButton?.matches(GRID_CELL) ? targetButton
        : targetButton?.parentElement?.querySelector<HTMLButtonElement>(GRID_CELL);
      if (!cell || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      const all = [...event.currentTarget.querySelectorAll<HTMLButtonElement>(GRID_CELL)];
      const grid = cell.closest<HTMLElement>(".picker-grid");
      const columns = grid ? getComputedStyle(grid).gridTemplateColumns.split(" ").length : 1;
      const index = all.indexOf(cell);
      const target = {
        ArrowLeft: index - 1, ArrowRight: index + 1, ArrowUp: index - columns, ArrowDown: index + columns,
        Home: 0, End: all.length - 1,
      }[event.key];
      event.preventDefault();
      if (target !== undefined) all[Math.min(Math.max(target, 0), all.length - 1)]?.focus();
    },
  };
}
