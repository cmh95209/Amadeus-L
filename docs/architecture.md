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
├── backend/
│   ├── main.py                # Flask app entry point
│   ├── api.py                 # REST API
│   ├── chat.py                # conversation pipeline (voice-first)
│   ├── memory.py              # SQLite conversation history and rolling summaries
│   ├── context_window.py      # Provider limits, prompt accounting, and history selection
│   ├── llm.py                 # LLM clients for built-in, local, and cloud models
│   ├── builtin_llm.py         # Built-in model tier and its separate prompt cap
│   ├── builtin_server.py      # Built-in runner and its launched context window
│   ├── tts.py                 # GPT-SoVITS voice client
│   ├── start_gptsovits.py     # voice engine startup
│   ├── ceremony.py            # first-meeting / wake-up logic
│   ├── ja_voice.py            # her Japanese voice rules
│   ├── cm_bridge.py           # bridge to the memory sidecar
│   ├── weather.py             # live weather / air quality
│   ├── webfetch.py            # web search result reading
│   ├── console_encoding.py    # UTF-8 console hardening
│   ├── stats.py               # trust / relationship stats
│   ├── environment.yml        # backend env (python 3.10)
│   ├── requirements.in        # backend dependencies
│   ├── assets/                # prerecorded interaction audio
│   └── data/                  # runtime data (git-ignored)
│
├── frontend/
│   ├── cubism/                # Live2D Cubism SDK (Core/Framework)
│   ├── public/
│   │   ├── cubism-shaders/
│   │   ├── live2d/            # character model assets
│   │   └── live2dcubismcore.min.js
│   ├── src/
│   │   ├── components/
│   │   │   ├── Live2DCharacter.tsx
│   │   │   └── SamplingSettings.tsx
│   │   ├── live2d/
│   │   │   ├── cubismBootstrap.ts
│   │   │   ├── KurisuController.ts
│   │   │   ├── KurisuModel.ts
│   │   │   ├── MotionPlayer.ts
│   │   │   └── textureLoader.ts
│   │   ├── audio/
│   │   │   └── SpeechPlayer.ts
│   │   ├── App.tsx
│   │   ├── api.ts
│   │   ├── interactions.ts
│   │   ├── main.tsx
│   │   └── styles.css
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
│
├── memory_sidecar/            # long-term memory (v2.0)
│   ├── cm_sidecar.py          # sidecar entry point (port 9870)
│   ├── requirements.txt       # pinned sidecar dependencies
│   ├── lib/                   # vendored CharacterMemory engine (GPL)
│   ├── seed_index/            # her lore, re-seeded on first start
│   ├── LICENSE-NOTE.md
│   └── venv/                  # built on first launch (git-ignored)
│
├── scripts/
│   ├── launcher.py            # cross-platform automatic launcher
│   ├── install_windows.ps1    # Windows one-shot installer (latest main)
│   ├── install_linux.sh       # Linux one-shot installer (latest main)
│   ├── install_macos.sh       # macOS one-shot installer (latest main)
│   └── make_pinned_installers.py  # builds each release's pinned installers
│
├── GPT-SoVITS/                # voice engine (cloned locally, git-ignored)
├── docs/images/
├── start_macos.command
├── start_windows.bat
├── check_amadeus.bat          # backend self-test (no model needed)
├── README.md
├── LICENSE
└── .gitignore
```

GPT-SoVITS is cloned separately into a local `GPT-SoVITS/` directory. It is
an external dependency rather than part of the Amadeus repository itself.

## Conversation context

Each answer call budgets the assembled prompt against both the chosen model
and the configured total cap. The bound output allowance and a safety margin
reduce the room for input. Instructions, cross-tab notes, memories, tool
schemas and results, and rendered attachments count toward that input.
OpenRouter endpoint limits are cached by model and routing policy. Custom
llama.cpp servers can report per-request `n_ctx` through `/props`, or set
`AMADEUS_LLAMA_N_CTX` in the backend environment. Built-in replies use the
launched tier's shared KV window.
[OpenRouter routing](https://openrouter.ai/blog/insights/model-routing/),
[llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

Published limits are keyed by exact model ID for OpenAI and Vertex.
[OpenAI model cards](https://developers.openai.com/api/docs/models),
[Vertex model cards](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models)

`chat.py` pins the conversation before assembly. `context_window.py` selects
whole recent exchanges and returns a `PromptHistory` whose IDs identify the
stored text sent in the answer call. `memory.py` maintains a bounded background
summary of the omitted prefix, using a history revision and the latest
requested cutoff when publishing. A larger cap restores fitting exchanges
on the next call. The reply pack exposes the successful call's stored IDs
as `verbatim_message_ids`.

The custom cap defaults to 16,384 tokens. A legacy custom value of 40,000
selects automatic mode, and other custom values are carried over as total
caps. Built-in tier defaults and legacy history overrides gain the old
10,096-token allowance during conversion.

---

## Technology

### Backend

- Python 3.10 (backend environment)
- Python 3.13 (memory sidecar environment)
- Flask
- SQLite
- Any OpenAI-compatible LLM server (local) or OpenRouter
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
