# WhaleReel

<img src="apps/web/public/brand/logo.png" width="88" alt="WhaleReel">

An infinite-canvas AI video production workbench. Arrange text, images, video, and audio on a free-form canvas, and run the complete AI short-drama pipeline: script parsing, per-episode review, asset extraction, storyboarding, first-frame generation, video generation, review, and final composition.

> This is the open-source code release. It does **not** include the internal design documents (`docs/`) or local runtime data. References to `docs/design/...` in code comments are internal document IDs kept for historical context.

## Highlights

- **Infinite canvas** — node-based composition of assets and generation tasks, with drag-and-drop connections, grouping, and version history; WebSocket progress updates in real time.
- **Short-drama pipeline** — script parsing → per-episode review → character/scene/prop assets → storyboard → first frame → video → review → composition/export.
- **Multiple model providers** — OpenAI-compatible APIs, Volcengine Ark (Seedream/Seedance), Vidu, ComfyUI local endpoints, and more. Configure providers and probe model lists in the admin UI; API keys are masked in responses.
- **Optional local inference** — resource-pool scheduling for local models such as MiniMax H3, Wan 2.2, Flux.1, and Qwen-Image. A mock mode lets you exercise the full pipeline without GPU dependencies.
- **Multi-user and data isolation** — built-in administrator account; business data is isolated by `owner_user_id`; unauthorized access returns 404.
- **Credits and billing (optional)** — pre-authorization/settlement, per-call billing, and automatic refunds on failure.
- **Responsive UI** — layouts for desktop, tablet, and phone.

## Tech stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.11+, FastAPI, SQLAlchemy, SQLite, WebSocket |
| Frontend | React 19, TypeScript, Vite 6, React Flow (@xyflow/react), zustand |
| Generation | OpenAI-compatible endpoints / Volcengine Ark / Vidu / ComfyUI / local Diffusers inference |

## Repository layout

```text
apps/web/                 Frontend (React + Vite)
  src/api/                REST and WebSocket client
  src/canvas/             Infinite canvas
  src/drama/              Short-drama workspace
  src/pages/              Login, admin, and other pages
  src/styles.css          Desktop and tablet styles
  src/styles-mobile.css   Phone styles

server/
  app/
    main.py               FastAPI entry point
    api/                  REST routes
    agent/                LLM prompts, skill packs, orchestration
    core/                 Config, providers, task engine, cache, billing
    db/                   ORM models and sessions
    runners/              Task runners
    script/               Script parsing and prompt assembly
    inference/            Local inference pipelines
  test_*.py               Standalone test scripts
  pyproject.toml          Backend dependencies
  .env.example            Configuration template
```

## Quick start

### 1. Start the backend

Python 3.11 or newer is required.

```bash
cd server
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .

# Copy the config template (Windows PowerShell: Copy-Item .env.example .env)
cp .env.example .env

uvicorn app.main:app --host 127.0.0.1 --port 8800
```

On first startup the server creates a SQLite database at `server/data/studio.db` and seeds a built-in administrator:

- Username: `admin`
- Password: `admin123`
- The UI forces a password change after the first login.

API docs: <http://127.0.0.1:8800/docs>

### 2. Start the frontend

Node.js 18 or newer is required.

```bash
cd apps/web
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api`, `/files`, and `/ws` to `127.0.0.1:8800`.

### 3. Run without a GPU or models

If no provider is configured and local inference dependencies are not installed, force mock mode to exercise the full pipeline:

```bash
# Windows
set AVS_MOCK=1
# macOS / Linux
export AVS_MOCK=1

uvicorn app.main:app --host 127.0.0.1 --port 8800
```

Mock mode produces placeholder images and videos, which is enough to validate the canvas, task queue, and short-drama workflow.

## Configure model providers

There are two options:

1. Copy `server/.env.example` to `server/.env` and fill in the values.
2. Sign in and open **系统管理 → 模型服务** (System → Model Services) to add keys and endpoints in the UI.

Each task type has its own provider configuration:

```text
AVS_LLM_PROVIDER / AVS_LLM_BASE_URL / AVS_LLM_API_KEY / AVS_LLM_MODEL
AVS_T2I_PROVIDER / AVS_T2I_BASE_URL / AVS_T2I_API_KEY / AVS_T2I_MODEL
AVS_T2V_PROVIDER / AVS_T2V_BASE_URL / AVS_T2V_API_KEY / AVS_T2V_MODEL
AVS_I2V_PROVIDER / AVS_I2V_BASE_URL / AVS_I2V_API_KEY / AVS_I2V_MODEL
AVS_TTS_PROVIDER / AVS_TTS_BASE_URL / AVS_TTS_API_KEY / AVS_TTS_MODEL
```

See `server/.env.example` for the full list. API keys are stored only in the local `.env` file or local SQLite database; list endpoints return only the first and last four characters.

### Local inference (optional)

```bash
cd server
pip install -e ".[inference]"
# Optional text-to-speech support
pip install -e ".[tts]"
```

Local inference requires an NVIDIA GPU and a matching CUDA build of PyTorch. Model weights are downloaded to `server/models/` (large; check free disk space and VRAM first). You can still use remote providers or mock mode without installing these dependencies.

## Run tests

Tests are standalone scripts. Run them from the repository root. On Windows:

```bash
server/.venv/Scripts/python server/test_auth_multitenant.py
server/.venv/Scripts/python server/test_billing.py
server/.venv/Scripts/python server/test_calls_log.py
server/.venv/Scripts/python server/test_comfyui.py
server/.venv/Scripts/python server/test_media_signature.py
server/.venv/Scripts/python server/test_model_skills.py
server/.venv/Scripts/python server/test_reliability_fixes.py
server/.venv/Scripts/python server/test_stream_events.py
server/.venv/Scripts/python server/test_version_trace.py
server/.venv/Scripts/python server/test_vidu.py
```

On macOS / Linux, replace `server/.venv/Scripts/python` with `server/.venv/bin/python`. The tests use temporary data directories and mock mode, so they do not touch `server/data/` and do not need external API keys.

## Deployment and security

- Change the default administrator password immediately after the first login.
- `server/.env`, `server/data/`, and `server/models/` are excluded by `.gitignore`; never commit keys or runtime data.
- For public deployments, use HTTPS behind a reverse proxy and restrict allowed origins with `AVS_CORS_ORIGINS`.
- Configure database backups, log rotation, and access control appropriate for your user base.

## Third-party code and licenses

This project is released under the MIT License. A few files contain third-party code; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md):

- `apps/web/src/ui/Aurora.tsx`: from React Bits, licensed under MIT + Commons Clause v1.0.
- `server/app/inference/hidream_o1/`: parts are derived from HiDream-O1-Image (MIT), HuggingFace Diffusers, Qwen, and Alibaba Wan (Apache-2.0); original copyright and license headers are retained.
- Frontend npm dependency licenses are listed in `apps/web/package-lock.json`.

## License

[MIT](LICENSE)

## Contributing

Issues and pull requests are welcome. Before submitting, run the tests above and make sure no keys, local databases, or generated artifacts are included.

## Repository status

This public repository is a one-way mirror of a private development repository. Code snapshots are exported with an allowlist, so internal design documents and local runtime data never enter this repository. Pull requests are welcome; accepted changes are integrated upstream and appear here on the next sync.
