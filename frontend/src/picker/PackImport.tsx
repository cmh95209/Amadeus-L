import { useRef, useState } from "react";
import { importPack, packProblem } from "./api";
import type { Pack } from "./api";
import { PickerMenu } from "./PickerControls";

const IMAGES = "image/png,image/webp,image/gif,image/jpeg";
const ARCHIVES = ".zip,.wastickers";

export function PackImport({ close, onAdded }: { close: () => void; onAdded: (pack: Pack) => void }) {
  const images = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const archive = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [folder, setFolder] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  function choose(input: HTMLInputElement, kind: "images" | "folder" | "archive") {
    const chosen = Array.from(input.files ?? []);
    input.value = "";
    if (!chosen.length) return;
    const folder = kind === "folder" ? chosen[0].webkitRelativePath.split("/")[0] : "";
    setFiles(chosen);
    setFolder(folder);
    setName(folder || (kind === "archive" ? chosen[0].name.replace(/\.(zip|wastickers)$/i, "") : ""));
    setProblem("");
  }

  async function add() {
    if (busy || !files.length || !name.trim()) return;
    setBusy(true);
    setProblem("");
    const body = new FormData();
    body.set("name", name.trim());
    if (folder) body.set("folder", folder);
    files.forEach((file) => body.append("files", file));
    try { onAdded((await importPack(body)).pack); }
    catch (error) { setProblem(packProblem(error)); }
    finally { setBusy(false); }
  }

  return (
    <PickerMenu label="Add sticker pack" close={close}>
      <div className="picker-menu-actions">
        <button type="button" disabled={busy} onClick={() => images.current?.click()}>Choose images</button>
        <button type="button" disabled={busy} onClick={() => folderInput.current?.click()}>Choose folder</button>
        <button type="button" disabled={busy} onClick={() => archive.current?.click()}>Choose archive</button>
        <input type="file" ref={images} multiple hidden accept={IMAGES}
          onChange={(event) => choose(event.currentTarget, "images")} />
        <input type="file" ref={folderInput} multiple hidden {...{ webkitdirectory: "" }}
          onChange={(event) => choose(event.currentTarget, "folder")} />
        <input type="file" ref={archive} hidden accept={ARCHIVES}
          onChange={(event) => choose(event.currentTarget, "archive")} />
      </div>
      <label>
        Pack name
        <input className="picker-search" value={name} disabled={busy}
          onChange={(event) => setName(event.currentTarget.value)}
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void add(); } }} />
      </label>
      {files.length > 0 && (
        <p className="picker-chosen-files">{files.map((file) => file.name).join(", ")}</p>
      )}
      <div className="picker-menu-actions">
        <button type="button" disabled={!files.length || !name.trim() || busy} onClick={() => void add()}>
          {busy ? "Importing…" : "Import pack"}
        </button>
      </div>
      {problem && <p role="status">{problem}</p>}
    </PickerMenu>
  );
}
