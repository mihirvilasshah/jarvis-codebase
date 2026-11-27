# Northwind Knowledge Galaxy

A cinematic 3D map of a small business's knowledge base. Thirty Markdown notes become
glowing, connected stars; clicking one flies the camera to it and opens its excerpt.
A chat bar answers questions using **only those notes**, through a FreeLLMAPI
(OpenAI-compatible) gateway. Chats can be collapsed, closed, and reopened, and
past conversations are saved in the browser so you can continue them later.

**Live demo:** <https://jarvis-codebase.vercel.app> — the 3D galaxy and
`GET /api/chat` health check work now. The Vercel project has the FreeLLMAPI
environment variables set, but its base URL currently points at localhost, which
Vercel cannot reach; see the gateway caveat below to enable chat there.

- Frontend: one static HTML file, Three.js + `3d-force-graph` from a pinned CDN. No npm, no build step.
- Indexer: `build.py`, Python 3 standard library only.
- Assistant: `brain.py` (shared), `server.py` (local), `api/chat.py` (Vercel).
- API key: server-side only. It is never sent to the browser.

## Quick start (local)

```powershell
python build.py
python server.py
```

Open <http://127.0.0.1:4700>. The server binds `127.0.0.1` only and serves the
`viewer/` directory plus `POST /chat`.

### 1. Start FreeLLMAPI

Run FreeLLMAPI locally (default gateway `http://localhost:3001/v1`) and copy your
unified key (`freellmapi-...`).

### 2. Create `config.json` in the project root

`config.json` is gitignored and must never be placed inside `viewer/`.

```json
{
  "freellmapi_api_key": "freellmapi-your-unified-key",
  "freellmapi_base_url": "http://localhost:3001/v1",
  "freellmapi_model": "auto"
}
```

Then restart `python server.py`. `GET /health` should report `"configured": true`.

If the key is missing or still a placeholder, the UI shows a clean
“not configured yet” message — no stack trace, no key, no partial answer.

## How it works

- `build.py` scans `notes/**/*.md`, derives each label from the filename, groups by
  parent folder, stores a ~700-character excerpt, and links notes that mention
  another note's title or share `[[wikilinks]]`.
- It writes `viewer/graph-data.js` (`const GRAPH = { nodes, links }`) for the browser
  and `viewer/graph.json` for the server. Node `id` is the array index.
- Retrieval is keyword overlap with a higher weight on titles, top 6 notes. The
  prompt includes only those titles/excerpts; the answer is limited to 2–3 sentences.
- Conversation history is bounded to the last 6 short turns. On Vercel the browser
  sends the active session's history with each request (no Python globals); the
  server sanitizes it and re-asserts the notes-only reminder. Locally the server
  also keeps a bounded in-memory fallback, but sanitized client history wins so
  restored sessions keep their context.
- `POST /chat` (local) and `POST /api/chat` (Vercel) return exactly:

```json
{ "answer": "...", "nodes": [3, 5, 9] }
```

`nodes` is `[]` when no note is relevant. Errors return `{ "error", "code", "nodes": [] }`
with a safe message for missing config, auth failure, rate limiting, timeout, and
malformed upstream responses.

## Chat controls and history

- **History** opens a browser-local list of saved conversations. Pick one to restore
  it, continue it, or delete it individually. **+ New chat** starts fresh, and
  **Clear all** removes every saved chat after a confirmation prompt.
- **Collapse** hides the transcript while leaving the input available; **Close**
  hides the whole panel and shows an “Ask your notes” launcher at the bottom of the
  screen. Sending a question automatically expands and reopens the chat.
- History lives in `localStorage` under `kg_chat_sessions_v1`, with the active
  session in `kg_active_session`. Up to 20 sessions and 60 messages per session are
  kept; only the last 6 messages are sent to the API. Collapse/close state is kept
  in `sessionStorage` (`kg_chat_collapsed`, `kg_chat_closed`) so it survives a
  reload but resets in a new tab.
- History is per browser and per origin. It is not synced to Vercel, and clearing
  site data removes it. Legacy `kg_session_id` / `kg_history` data is migrated
  automatically on first load.
- Buttons are disabled while a request is in flight so a reply cannot land in the
  wrong conversation.

## Deploy to Vercel

The repository is ready: `vercel.json` builds the graph, publishes `viewer/` as
static output, and bundles `api/chat.py` together with `brain.py` and
`viewer/graph.json`.

### Option A — CLI

```powershell
npx vercel@latest login
npx vercel@latest --prod
```

Set the environment variables in the Vercel project (Production and Preview):

| Name | Value |
| --- | --- |
| `FREELLMAPI_API_KEY` | your `freellmapi-...` unified key |
| `FREELLMAPI_BASE_URL` | a reachable HTTPS gateway ending in `/v1` |
| `FREELLMAPI_MODEL` | `auto` (or a model the gateway exposes) |

`config.json` is intentionally gitignored and is **not** uploaded by the CLI when
ignored. Vercel reads the environment variables instead.

### Option B — Dashboard

1. Push this folder to a Git repository (without `config.json`).
2. In Vercel, **Add New → Project**, import the repo.
3. Framework preset: **Other**. Build command: `python3 build.py`. Output directory: `viewer`.
4. Add the three environment variables above, then deploy.

### Important FreeLLMAPI caveat for Vercel

FreeLLMAPI is designed to run locally, and its docs advise against exposing the
proxy publicly. `http://localhost:3001/v1` is unreachable from Vercel's servers.
For a deployed instance that actually answers questions you need a hosted
OpenAI-compatible gateway, or a temporary HTTPS tunnel to your machine
(`cloudflared tunnel --url http://localhost:3001`, `ngrok http 3001`) set as
`FREELLMAPI_BASE_URL`. If you keep the default local URL, the deployment is still
fully functional as a 3D site and the chat endpoint returns a clean connection
error instead of leaking anything.

## Security notes

- The key lives in `config.json` or Vercel env vars, never in `viewer/`, HTML, or JS.
- All FreeLLMAPI calls happen server-side; the browser only calls `/chat` or `/api/chat`.
- `viewer/` is the only directory the local server can serve; `config.json` and notes
  are not exposed.
- CORS is restricted to same-origin requests; request bodies are capped at 64 KiB.
- Responses are `Cache-Control: no-store` and include `nosniff` / `no-referrer` headers.

## Verification

```powershell
python -m py_compile build.py brain.py server.py api\chat.py
python build.py
```

Expected: `Indexed 30 notes across 6 groups with 90 links.`

Try these questions after configuring a key:

- “What are the café’s opening hours?”
- “How much is a 12 oz latte?”
- “What is our cash runway?”

Without a key, each of these should return the clean “not configured” message.

Deployment checks already performed:

- Homepage returns 200 and renders 30 nodes / 90 links / 6 groups with no console errors.
- The History, Collapse, and Close controls work on the deployed site; saved sessions
  restore across reloads, and the launcher reopens a closed chat.
- `POST /api/chat` returns a clean `503 config_invalid` while `FREELLMAPI_BASE_URL`
  points at localhost; no key or internal detail leaks.
- `/config.json` returns 404 on both local and Vercel; no key literal appears in HTML/JS.

## The Bay - REA website observation

Jarvis can observe public websites with [REA](https://github.com/morluto/rea)
(MIT, npm `rea-agents`). It is installed **outside** this project, pinned, and
invoked by absolute path so a server started without your shell `PATH` still finds it.

Exact install commands used, after confirming
`npm view rea-agents repository.url` returned `git+https://github.com/morluto/rea.git`:

```powershell
$dir = Join-Path $env:USERPROFILE '.jarvis-tools\rea'
New-Item -ItemType Directory -Path $dir -Force
Set-Location $dir
npm init -y
npm install rea-agents@6.10.1 --save-exact --ignore-scripts
```

`teardown.py` is standard-library only and soft-imported, so Jarvis boots even
if it is absent. `GET /teardown/diag` reports readiness, installed and pinned
versions, Node version, Chrome discovery, and plain-English problems. `POST
/teardown` with `{"url": "https://..."}` (or the chat tag `[TEARDOWN:https://...]`)
observes one website through `capture-browser-scenario` using a fixed `goto` +
`wait` scenario. Missing pieces produce one in-character line saying what to install.

**The law:** REA observes only. The scenario never clicks, types, logs in, or
changes anything, and the URL gate refuses non-http(s), local/private/loopback
addresses, embedded credentials, and interactive commands. Only public websites
you may look at are accepted.

## Preflight (live end-to-end check)

Run this before calling any change done:

```powershell
python build.py
python server.py          # in another terminal, and leave it running
python preflight.py
```

`preflight.py` makes real calls to the running server and the FreeLLMAPI gateway;
it uses no mocks and no unit tests. In order it verifies the viewer is served,
the graph loads, `/chat` answers with a real node list, the API key is accepted,
the configured model id is reachable through the Models API, `/remember` writes
a note that `/chat` retrieves immediately (then deletes it, rebuilds the graph
and calls `POST /reindex`), served files are byte-identical to the files on
disk, `config.json` is unreachable even through encoded traversal, and a real
headless tab with `?mute=1` never reaches speech synthesis.

It finishes with a line like `9 pass, 0 fail, 0 warn` and exits non-zero on any
failure. The mute check needs Chrome or Edge and the optional `websockets`
package; without them it prints a warning instead of failing.

> Note: `vercel curl` was used once for verification and added a Deployment Protection
> automation bypass secret to the project. Revoke it under **Project Settings →
> Deployment Protection → Automation Bypass** if you do not need it.

## Project layout

```
api/chat.py            Vercel serverless handler (POST /api/chat)
notes/                 30 Markdown notes in 6 folders
viewer/index.html      single-page 3D viewer + chat UI
viewer/favicon.svg     site icon, with favicon.ico and touch PNG fallbacks
viewer/graph-data.js   generated browser graph data
viewer/graph.json      generated server-side graph data
brain.py               config, retrieval, prompt, FreeLLMAPI, history
build.py               standard-library indexer
server.py              local 127.0.0.1:4700 server
teardown.py            read-only REA website observation bay (soft-imported)
preflight.py           live end-to-end harness (exit non-zero on failure)
config.example.json    config template (safe to commit)
config.json            your local secrets (gitignored)
vercel.json            build, output, function, headers
requirements.txt       no third-party packages
```
