# Live Web Research Implementation

## Status
- [x] Intent detection and voice routing
- [x] DuckDuckGo, SearXNG and Wikipedia provider abstraction
- [x] Safe webpage extraction and bounded concurrency
- [x] Ollama JSON analysis with source-only fallback
- [x] Structured research card and history/export actions
- [x] SQLite research sessions
- [ ] Browser automation fallback for JavaScript-only pages
- [ ] Full live external end-to-end verification

## Architecture decisions
- Keep the existing browser SpeechRecognition and SpeechSynthesis pipeline.
- Use provider-independent Python modules; no paid API is required.
- Use `ddgs` first, then SearXNG and Wikipedia as fallback sources.
- Preserve raw search/extraction fallback when Ollama is unavailable.
- Store compact excerpts and metadata in SQLite, not full webpages.
