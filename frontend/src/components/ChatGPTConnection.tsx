import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  CHATGPT_REASONING_LEVELS,
  CHATGPT_USAGE_URL,
  getChatGPTModels,
  getChatGPTStatus,
  isLocalPage,
  seeChatGPTWelcome,
  setChatGPTBackground,
  setChatGPTModel,
  setChatGPTReasoning,
  signOutChatGPT,
  startChatGPTSignIn,
} from "../api";
import type { ChatGPTModel, ChatGPTReasoningLevel, ChatGPTStatus } from "../api";

// Sign-in must happen on the PC (OpenAI sends the browser back to 127.0.0.1).
// Any *.localhost name is this PC too (test copies use one to reach their own
// backend), the same rule as the backend's origin_guard.is_local_name.
const onThisPC = isLocalPage ||
  (typeof window !== "undefined" && window.location.hostname.toLowerCase().endsWith(".localhost"));

const PRIVACY_KEY = "amadeus.chatgptPrivacySeen";
const POLL_MS = 2_000;
const SIGN_IN_TIMEOUT_MS = 10 * 60_000;

export const CHATGPT_REASONING_LABELS: Record<ChatGPTReasoningLevel, string> = {
  low: "Light",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max",
};

function privacySeen() {
  try { return localStorage.getItem(PRIVACY_KEY) === "1"; } catch { return false; }
}

type Props = {
  active: boolean;
  disabled: boolean;
  connection: ReturnType<typeof useChatGPTConnection>;
  onSelect: () => Promise<void>;
  onConnectionChanged: () => void;
  children: ReactNode;
};

export function useChatGPTConnection(active: boolean, settingsOpen: boolean) {
  const [status, setStatus] = useState<ChatGPTStatus | null>(null);
  const [models, setModels] = useState<ChatGPTModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [requestedLevel, setRequestedLevel] = useState<ChatGPTReasoningLevel | null>(null);
  const updating = useRef(false);
  const [error, setError] = useState("");
  const hasStatus = status !== null;
  const signedIn = !!status?.signed_in;
  const needStatus = active || settingsOpen;

  const report = useCallback((err: unknown) => {
    setError(err instanceof Error ? err.message : "Could not connect to ChatGPT.");
  }, []);

  useEffect(() => {
    if (!needStatus) return;
    const controller = new AbortController();
    void getChatGPTStatus(controller.signal)
      .then((info) => { if (!controller.signal.aborted) setStatus(info); })
      .catch((err) => { if (!controller.signal.aborted) report(err); });
    return () => controller.abort();
  }, [needStatus, report]);

  useEffect(() => {
    if (!active || !signedIn) {
      setModels([]);
      return;
    }
    const controller = new AbortController();
    void getChatGPTModels(controller.signal).then(({ models: items, ...choices }) => {
      if (controller.signal.aborted) return;
      setModels(items);
      setStatus((current) => current && { ...current, ...choices });
    }).catch((err) => { if (!controller.signal.aborted) report(err); });
    return () => controller.abort();
  }, [active, signedIn, report]);

  const update = useCallback(async (action: () => Promise<Partial<ChatGPTStatus>>, onSuccess?: () => void) => {
    if (!hasStatus || updating.current) return;
    updating.current = true;
    setBusy(true);
    setError("");
    try {
      const changes = await action();
      setStatus((current) => current && { ...current, ...changes });
      onSuccess?.();
    } catch (err) {
      report(err);
    } finally {
      updating.current = false;
      setBusy(false);
    }
  }, [hasStatus, report]);

  // Keep the range focused while a save is in flight. A new input replaces
  // the pending choice, and the latest choice is saved when the request ends.
  const savedLevel = status?.level ?? "low";
  useEffect(() => {
    if (requestedLevel === null || busy) return;
    if (!active || !signedIn || requestedLevel === savedLevel) {
      setRequestedLevel(null);
      return;
    }
    void update(async () => {
      try {
        return await setChatGPTReasoning(requestedLevel);
      } finally {
        setRequestedLevel((current) => current === requestedLevel ? null : current);
      }
    });
  }, [active, busy, requestedLevel, savedLevel, signedIn, update]);

  const model = status?.model ?? "";
  const publishedLevels = status?.reasoning_levels
    ? status.reasoning_levels[model]
    : models.find((option) => option.id === model)?.reasoning_levels;
  const levels = Array.isArray(publishedLevels) ? publishedLevels : CHATGPT_REASONING_LEVELS;
  const reasoning = {
    levels,
    level: requestedLevel ?? savedLevel,
    disabled: !signedIn || models.length === 0 || levels.length === 0 || (busy && requestedLevel === null),
    choose: setRequestedLevel,
  };
  const chooseModel = (model: string) => void update(() => setChatGPTModel(model));
  return { status, setStatus, models, busy: busy || requestedLevel !== null, error, setError,
    report, update, chooseModel, reasoning };
}

function ChatGPTModelSelect({ connection, disabled }: {
  connection: ReturnType<typeof useChatGPTConnection>;
  disabled: boolean;
}) {
  const { status, models, busy, chooseModel } = connection;
  const model = status?.model ?? "";
  return (
    <select value={model} disabled={busy || disabled || !status?.signed_in || models.length === 0}
      onChange={(event) => chooseModel(event.target.value)}>
      {!models.some((option) => option.id === model) && (
        <option value={model} translate={model ? "no" : undefined}>{model || "Choose a model"}</option>
      )}
      {models.map((option) => <option key={option.id} value={option.id} translate="no">{option.name}</option>)}
    </select>
  );
}

export default function ChatGPTConnection({ active, disabled, connection, onSelect, onConnectionChanged, children }: Props) {
  const { status, setStatus, busy, error, setError, report, update } = connection;
  const [privacy, setPrivacy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const welcomeTitleRef = useRef<HTMLHeadingElement>(null);
  const signIn = useRef<AbortController | null>(null);
  const signedIn = !!status?.signed_in;
  const choiceDisabled = disabled || waiting || busy || (!status && !error);
  const welcome = active && signedIn && status?.welcome_seen === false;
  const hasDialog = privacy || welcome;

  useEffect(() => () => signIn.current?.abort(), []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || !hasDialog) return;
    dialog.showModal();
    welcomeTitleRef.current?.focus();
    return () => dialog.close();
  }, [hasDialog]);

  async function openSignIn() {
    setPrivacy(false);
    setError("");
    if (!onThisPC) {
      setWaiting(true);
      try {
        await startChatGPTSignIn(new AbortController().signal);
      } catch (err) {
        report(err);
      } finally {
        setWaiting(false);
      }
      return;
    }
    // Open within the click so browser popup blocking doesn't stop sign-in.
    const tab = window.open("about:blank", "_blank");
    if (!tab) {
      setError("Allow pop-ups to sign in with ChatGPT.");
      return;
    }
    tab.opener = null;
    const controller = new AbortController();
    signIn.current = controller;
    setWaiting(true);
    let pollTimer: number | undefined;
    const timeout = window.setTimeout(() => {
      controller.abort();
      setWaiting(false);
      setError("ChatGPT sign-in timed out. Try again.");
    }, SIGN_IN_TIMEOUT_MS);
    controller.signal.addEventListener("abort", () => {
      window.clearTimeout(pollTimer);
      window.clearTimeout(timeout);
    }, { once: true });

    async function poll() {
      try {
        const info = await getChatGPTStatus(controller.signal);
        if (controller.signal.aborted) return;
        if (info.signed_in) {
          window.clearTimeout(timeout);
          setStatus(info);
          setWaiting(false);
          try { localStorage.setItem(PRIVACY_KEY, "1"); } catch { /* Browser storage is optional. */ }
          if (active) onConnectionChanged();
          else await onSelect();
          return;
        }
        if (info.sign_in_error) {
          window.clearTimeout(timeout);
          setWaiting(false);
          setError(info.sign_in_error);
          return;
        }
      } catch {
        // The backend can be momentarily unavailable while the browser signs in.
      }
      if (!controller.signal.aborted) pollTimer = window.setTimeout(() => void poll(), POLL_MS);
    }

    try {
      const { url } = await startChatGPTSignIn(controller.signal);
      if (controller.signal.aborted) {
        tab.close();
        return;
      }
      tab.location.replace(url);
      pollTimer = window.setTimeout(() => void poll(), POLL_MS);
    } catch (err) {
      tab.close();
      if (!controller.signal.aborted) {
        window.clearTimeout(timeout);
        setWaiting(false);
        report(err);
      }
    }
  }

  function select() {
    if (signedIn) void onSelect();
    else if (!onThisPC || privacySeen() || status?.welcome_seen) void openSignIn();
    else setPrivacy(true);
  }

  function chooseWelcomeBackground(allowed: boolean) {
    void update(async () => {
      const background = await setChatGPTBackground(allowed);
      setStatus((current) => current && { ...current, ...background });
      const seen = await seeChatGPTWelcome();
      return seen;
    });
  }

  return (
    <>
      <div className="brain-choice-buttons">
        {children}
        <button type="button" className="chatgpt-brain-choice" aria-pressed={active}
          disabled={choiceDisabled} onClick={select}>
          <img src="/icons/chatgpt/chatgpt-logo-white.svg" width={16} height={16} alt="" />
          {signedIn ? "ChatGPT (use your ChatGPT plan)" : "Sign in with ChatGPT"}
        </button>
      </div>
      {waiting && <p className="settings-help" role="status">Waiting for ChatGPT sign-in...</p>}
      {active && (
        <div className="chatgpt-connection">
          {!signedIn && !waiting && (
            <button type="button" className="primary-button chatgpt-sign-in" disabled={choiceDisabled} onClick={select}>
              Sign in
            </button>
          )}
          <p className="settings-help">Needs a paid ChatGPT plan (Plus or Pro)</p>
          {signedIn && status && (
            <>
              <div className="chatgpt-account">
                <span>{`Signed in as ${status.email}`}</span>
                <button type="button" className="ghost-button" disabled={busy || disabled}
                  onClick={() => void update(signOutChatGPT, onConnectionChanged)}>Sign out</button>
              </div>
              <label>
                Model
                <ChatGPTModelSelect connection={connection} disabled={disabled} />
              </label>
              <label>
                Reasoning
                <select value={connection.reasoning.level} disabled={disabled || connection.reasoning.disabled}
                  onChange={(event) => connection.reasoning.choose(event.target.value as ChatGPTReasoningLevel)}>
                  {connection.reasoning.levels.length === 0 && (
                    <option value={connection.reasoning.level}>Reasoning unavailable</option>
                  )}
                  {connection.reasoning.levels.map((level) => (
                    <option key={level} value={level}>{CHATGPT_REASONING_LABELS[level]}</option>
                  ))}
                </select>
              </label>
              <div className="settings-help chatgpt-background">
                <span>{status.background_use_allowed ? "All features are on." : "Some features are off (greetings, picture reactions, memory learning)."}</span>
                <button type="button" className={status.background_use_allowed ? "chatgpt-turn-off" : "ghost-button"}
                  disabled={busy || disabled}
                  onClick={() => void update(() => setChatGPTBackground(!status.background_use_allowed))}>
                  {status.background_use_allowed ? "Turn off" : "Allow all features"}
                </button>
              </div>
              <a href={CHATGPT_USAGE_URL} target="_blank" rel="noreferrer">Check your ChatGPT usage</a>
            </>
          )}
        </div>
      )}
      {error && <p className="settings-error" role="alert">{error}</p>}
      {hasDialog && (
        <dialog className="modal native-modal" ref={dialogRef} aria-labelledby="chatgpt-dialog-title"
          onKeyDown={(event) => event.stopPropagation()}
          onCancel={(event) => {
            event.preventDefault();
            if (privacy) setPrivacy(false);
          }}>
          <div className="modal-heading">
            <h2 id="chatgpt-dialog-title" ref={privacy ? undefined : welcomeTitleRef} tabIndex={privacy ? undefined : -1}>
              {privacy ? "Before you sign in" : "You're using your ChatGPT plan"}
            </h2>
          </div>
          {privacy ? (
            <>
              <p>Amadeus signs in with your ChatGPT account so her replies use your ChatGPT plan.</p>
              <p>Your password goes only to OpenAI.</p>
              <p>Your chats, her memory and the sign-in stay on this PC. The sign-in is locked to your Windows account.</p>
              <p>Your messages to her are sent to OpenAI to get her replies, under OpenAI's terms. Nothing is sent to the Amadeus developers.</p>
              <p>You can sign out at any time.</p>
            </>
          ) : (
            <>
              <p>Eligible usage in this app uses your ChatGPT plan.</p>
              <p>Amadeus needs your consent to allow all features to work on ChatGPT, such as picture reactions, greetings and memory learning.</p>
              <p>If you reject, she can only chat and text normally, with some features missing.</p>
            </>
          )}
          {!privacy && error && <p className="settings-error" role="alert">{error}</p>}
          <div className="modal-actions">
            {privacy ? (
              <>
                <button type="button" className="ghost-button" onClick={() => setPrivacy(false)}>Cancel</button>
                <button type="button" className="primary-button" onClick={() => void openSignIn()}>Continue</button>
              </>
            ) : (
              <>
                <button type="button" className="ghost-button" disabled={busy}
                  onClick={() => chooseWelcomeBackground(false)}>Reject</button>
                <button type="button" className="primary-button" disabled={busy}
                  onClick={() => chooseWelcomeBackground(true)}>Allow all features</button>
              </>
            )}
          </div>
        </dialog>
      )}
    </>
  );
}
