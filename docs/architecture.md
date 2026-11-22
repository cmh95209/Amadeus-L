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
│   ├── attachments.py         # attachment preparation and model previews
│   ├── memory.py              # SQLite conversation history
│   ├── llm.py                 # LLM access (local or OpenRouter)
│   ├── chatgpt_auth.py         # ChatGPT sign-in, token renewal and consent
│   ├── chatgpt_llm.py          # ChatGPT Responses requests and background call limits
│   ├── origin_guard.py         # browser and local sign-in request checks
│   ├── tts.py                 # GPT-SoVITS voice client
│   ├── start_gptsovits.py     # voice engine startup
│   ├── ceremony.py            # first-meeting / wake-up logic
│   ├── ja_voice.py            # her Japanese voice rules
│   ├── cm_bridge.py           # bridge to the memory sidecar
│   ├── memory_plan_bridge.py   # local endpoint for sidecar learning on the ChatGPT plan
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

## ChatGPT plan connection

Sign-in starts from a local app page and finishes on the backend's loopback
callback. Consent and the welcome screen belong to one sign-in. Replies send
the leading persona and fixed rules as Responses `instructions` by default.
Later system notes stay in order as developer input. Voice-first replies write
the Japanese line before a separate display translation. Attachment reactions
can see ready pictures, document excerpts, and ready video stills before send
when the user allows optional features on the ChatGPT plan.
If OpenAI rejects an access token early, the backend refreshes it once and
retries the request with the current saved token.

When ChatGPT reports a plan limit, background model calls pause for 30 minutes
or until a user-message call succeeds. The memory bridge returns HTTP 429
during the pause so the sidecar can keep the batch for a future attempt. Stored
voice replay accepts audio files beneath `backend/generated/` and `backend/assets/`.

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
