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
