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
├── backend/                   # Flask backend: chat pipeline, memory, model access, voice client
│   ├── main.py                # Flask app entry point
│   ├── api.py                 # REST API
│   ├── chat.py                # conversation pipeline (voice-first)
│   ├── attachments.py         # attachment preparation and model previews
│   ├── memory.py              # SQLite conversation history and rolling summaries
│   ├── context_window.py      # Provider limits, prompt accounting, and history selection
│   ├── cross_tab.py           # Other-tab catch-up and transcript recall across chats
│   ├── moment_recall.py       # Scene boundaries, recall policy, cadence and rendering
│   ├── transcript_vector_index.py # Disposable SQLite transcript embeddings and lookup worker
│   ├── llm.py                 # LLM clients for built-in, local, and cloud models
│   ├── builtin_llm.py         # Built-in model tier and its separate prompt cap
│   ├── builtin_server.py      # Built-in runner and its launched context window
│   ├── webpdf.py              # bounded extraction of PDF search results
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
│   ├── tests/
│   │   └── test_cm_learning.py  # offline learning and recovery tests
│   └── data/                  # runtime data (git-ignored)
│
├── frontend/                  # React / TypeScript page with the Live2D character
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
├── memory_sidecar/            # long-term memory service (separate process)
│   ├── cm_sidecar.py          # sidecar entry point (port 9870)
│   ├── requirements.txt       # pinned sidecar dependencies
│   ├── lib/                   # vendored CharacterMemory engine (GPL)
│   │   └── character_memory/memory/
│   │       └── learning_jobs.py  # SQLite queue, extraction checkpoints, legacy catch-up
│   ├── seed_index/            # her lore, re-seeded on first start
│   ├── LICENSE-NOTE.md
│   └── venv/                  # built on first launch (git-ignored)
│
├── scripts/                   # launcher, installers, release and language tools
│   ├── launcher.py            # cross-platform automatic launcher
│   ├── install_windows.ps1    # Windows one-shot installer (latest main)
│   ├── install_linux.sh       # Linux one-shot installer (latest main)
│   ├── install_macos.sh       # macOS one-shot installer (latest main)
│   └── make_pinned_installers.py  # builds each release's pinned installers
│
├── GPT-SoVITS/                # voice engine (cloned locally, git-ignored)
├── docs/                      # these guides
│   └── images/
├── start_*.bat / .sh / .command   # one-click start files
├── check_amadeus.bat          # backend self-test (no model needed)
├── README.md
├── LICENSE
└── .gitignore
```

For a file-by-file map of the code (what each module owns, how one message
travels, where settings and data live, and where to start for a change), see
[ARCHITECTURE.md](../ARCHITECTURE.md) at the top of the repository.

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

**Search old chats** uses those IDs to search earlier messages in the current
chat and messages in other chats. It pairs user turns with their active
replies before masking, checks every proposed row against the current
transcript, and excludes discarded versions, private or ineligible rows,
future rows and quotes already in the assembled prompt. A reply gets up to
one scene from its source tab. It can include up to three exchanges on each
side of the matched exchange. Turn gaps, a gap of 30 minutes or more,
privacy boundaries and clear topic changes can stop expansion. Uncertain
timestamps limit that side to one neighbor.

The scene gets up to 8% of the effective input ceiling, with a cap of 1,200
tokens on the built-in model and 6,000 elsewhere. Actual remaining request
space is the hard limit. The renderer trims outer exchanges first, then
clips around the matched passage if the anchor is still too large. Speaker,
date, relative age and source-tab labels go in the per-reply note, or in
the changing section of a custom local server's system message. One fixed
instruction gives precedence to the user's current words. The stable
prefix and selected verbatim history are byte-identical with and without
recall. The separate catch-up setting still covers other tabs.

Spontaneous recall needs strong evidence. The thresholds
are cosine 0.62 with a 0.08 lead over the next distinct scene, or cosine 0.55
with two distinct lexical cues including a rare cue. A third path accepts
cosine 0.515 with a 0.04 lead and three independent cue groups. Callbacks require three
intervening user turns and are limited to two per 12 user turns across tabs.
The in-process ledger keys retries and Regenerate to the same user turn and
prevents repeating a scene in that window. Greetings omit scene recall.
Direct recall questions bypass cadence and can use a hedged match at
cosine 0.48 with a 0.08 lead. Sensitive material requires a user-led request. Multilingual intent
and sensitivity checks use example embeddings. Keyword evidence remains
available when there is no vector hit and direct intent is known.

`transcript_vector_index.py` reconciles the newest 50,000 eligible rows in
a disposable `transcript_vectors.sqlite3`. Text changes, active versions
and deletions invalidate individual rows. Schema and embedding configuration
changes rebuild the cache. The backend submits up to four rows at a time
to `POST /embed/transcript` and polls the sidecar's single job slot by ID.
A lost request leaves encoding in that slot. An active job logs a stall at
120 seconds and stays pollable. Each successful batch commits verified rows
and leaves a one-second pause. Failures back off for 15, 30, 60 and 120
seconds, then pause indexing after the fifth failure without a commit.
Restarting or re-enabling search resumes indexing. Reply queries and memory
learning have priority at embedder admission. Query and indexing backoffs
are independent, and a reply waits for semantic evidence at most 250 ms.

The custom cap defaults to 65,536 tokens for cloud models and 16,384 for local
servers. A legacy custom value of 40,000
selects automatic mode, and other custom values are carried over as total
caps. Built-in tier defaults and legacy history overrides gain the old
10,096-token allowance during conversion.

The memory sidecar's background worker reloads provider settings for each
extraction and resumes due SQLite jobs after startup. It checkpoints model
results before applying them and marks source messages processed after the
memory writes and index publication succeed. A one-time migration queues a
bounded window of previously processed messages for recovery.
The queue lives in
[`learning_jobs.py`](../memory_sidecar/lib/character_memory/memory/learning_jobs.py).
Its offline coverage lives in [`test_cm_learning.py`](../backend/tests/test_cm_learning.py).

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
during the pause so the sidecar's durable queue keeps the batch pending without
using a failure attempt. Stored voice replay accepts audio files beneath
`backend/generated/` and `backend/assets/`.

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
