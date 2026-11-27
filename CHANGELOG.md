# Changelog

## Local action hub
- Added confirmed website-opening commands and optional local Ollama
  interpretation for less predictable browser requests.
- Added a confirmation-gated Windows Firefox launch command.
- Added Chrome, Edge, default-browser and new-tab voice commands; unsupported
  actions no longer open the tasks panel.
- Added explicit HUD voice and typed actions with clarification for unknown commands.
- Added local SQLite tasks and one-time reminders with browser-only due alerts.
- Added same-origin protections for local action mutations and offline tests.

## Live web research
- Added free live web research with DuckDuckGo/DDGS, SearXNG and Wikipedia fallbacks.
- Added SSRF-safe URL validation and bounded parallel content extraction.
- Added optional Ollama analysis and a raw-source fallback.
- Added research card UI, voice routing, SQLite history, saving and Markdown export.
