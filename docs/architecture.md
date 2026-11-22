# Architecture and Project Layout

How the pieces fit together, what lives where, and the stack behind each component.

## Architecture

Amadeus is split into five runtime components (the LLM server is external):

```text
┌─────────────────────────────────────────────┐
│ React / TypeScript WebUI                    │
│                                             │
│  Chat UI          Live2D Cubism / WebGL    │
└───────────────────────┬─────────────────────┘
                        │ HTTP
                        ▼
┌─────────────────────────────────────────────┐
│ Python / Flask Backend                      │
│                                             │
│  Chat   Memory   LLM   TTS API             │
└──────────┬──────────────────┬───────────────┘
           │                  │
           ▼                  ▼
┌─────────────────────┐  ┌──────────────────────────────────┐
│ GPT-SoVITS          │  │ CharacterMemory sidecar          │
│ Character voice     │  │ Long-term memory (separate       │
│ synthesis           │  │ process; GPL engine; optional)   │
└─────────────────────┘  └──────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────┐
│ LLM server (yours: local or cloud)          │
│ Unsloth / Ollama / LM Studio / llama.cpp /  │
│ vLLM / OpenRouter / any OpenAI-compatible   │
└─────────────────────────────────────────────┘
```

Default local services:

```text
GPT-SoVITS voice     http://127.0.0.1:9880
Amadeus backend      http://127.0.0.1:5050
Amadeus WebUI        http://127.0.0.1:5173
Memory sidecar       http://127.0.0.1:9870
LLM server           (yours — local port or cloud URL)
```

The WebUI on 5173 is a built copy of `frontend/` that the launcher prepares
(into `frontend/dist`) whenever the page's code changed, served with
`vite preview`; Vite's developer server is the fallback, and stays in use when
`.runtime/page_mode.txt` says `dev` (or `AMADEUS_PAGE_DEV=1`).

The AI backend is intentionally independent from the frontend. This allowed
the original Unity interface to be replaced by a browser-based React/WebGL
frontend without rewriting the conversational core, and the memory engine to
live in its own process.

---

## Project Structure

```text
Amadeus-Project/
├── backend/          # Flask backend: chat pipeline, memory, model access, voice client
├── frontend/         # React / TypeScript page with the Live2D character
├── memory_sidecar/   # long-term memory service (separate process)
├── scripts/          # launcher, installers, release and language tools
├── docs/             # these guides
├── GPT-SoVITS/       # voice engine (cloned locally, git-ignored)
└── start_*.bat / .sh / .command   # one-click start files
```

For a file-by-file map of the code (what each module owns, how one message
travels, where settings and data live, and where to start for a change), see
[ARCHITECTURE.md](../ARCHITECTURE.md) at the top of the repository.

GPT-SoVITS is cloned separately into a local `GPT-SoVITS/` directory. It is
an external dependency rather than part of the Amadeus repository itself.

---

## Technology

### Backend

- Python 3.10 (backend environment)
- Python 3.13 (memory sidecar environment)
- Flask
- SQLite
- Any OpenAI-compatible LLM server (local) or a cloud provider (OpenRouter, Google Vertex AI and others)
- GPT-SoVITS
- CharacterMemory (vendored, GPL-3.0 — isolated in the sidecar process)

### Frontend

- React 19
- TypeScript
- Vite
- WebGL
- Live2D Cubism SDK for Web

The Live2D runtime does not require users to install Cubism Editor or Unity.
The required Web runtime files and shaders are included with the project
frontend.
