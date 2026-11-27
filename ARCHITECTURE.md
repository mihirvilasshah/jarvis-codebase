# Architecture

## Live research

`research.py` contains intent detection, search provider fallbacks, URL and SSRF validation,
parallel webpage extraction, Ollama analysis, SQLite history and Markdown export. The existing
`server.py` and `api/chat.py` expose it through `/research` and `/api/research`.

The browser uses the existing chat/voice loop. Research sentences are detected in `ask()` and
sent to the research endpoint. Results are rendered in a validated research card, while
SpeechRecognition/SpeechSynthesis continue to own listening and playback.

Webpage and search-provider data is untrusted. The extractor strips scripts/navigation and
Ollama prompts explicitly prohibit instructions in retrieved text.

## Local action hub

The HUD uses the existing single browser recognition instance; explicit action
commands are parsed by `viewer/action-commands.js`. Unrecognized action
commands can be classified by an optional loopback Ollama model via
`action_router.py`; unsupported or ambiguous results ask for clarification.
The model may only propose an allowlisted action and browser, while website
addresses are extracted from the user's text and validated independently.
The notes microphone, research and REA retain their
separate paths. The task/reminder panel provides equivalent typed controls.

`server.py` serves the local-only `/actions` JSON API, while `actions.py` stores
items in a separate SQLite database outside `viewer/`. Mutations require the
loopback host, matching Origin, JSON content type and a custom request header.
Due times are timezone-aware UTC instants. The open browser polls for due
items; an atomic SQLite claim prevents duplicate browser notification attempts.
Due items remain visible until dismissed, including after a restart. There is
no background notification process and no action database on Vercel.
Browser launch is a local Windows-only allowlisted action: the UI asks for
confirmation, and the server locates Firefox, Chrome, or Edge without accepting
arbitrary command-line arguments or executable paths. HTTP(S) navigation
URLs are validated before passing them as a single browser argument; the
server does not fetch them. Generic browser and tab requests use the Windows
default when it is one of these browsers. The browser determines the window
that receives a new tab.
