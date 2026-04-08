# Release Notes

## v1.0.0 — Initial Release

### Features

- **Image-to-Lucidchart pipeline** — Upload an image of a diagram and automatically convert it to an editable Lucidchart document
- **Multi-provider AI support** — Gemini (`gemini-2.0-flash`), OpenAI (`gpt-4o`), Claude (`claude-sonnet-4-20250514`), xAI/Grok (`grok-4.20-reasoning`)
- **Lucidchart Standard Import API** integration — creates diagrams automatically from AI analysis
- **Drag & drop / browse** image upload (PNG, JPEG, JPG)
- **Couchbase Lite CE** (C SDK + Python CFFI bindings) for local embedded storage — no external database required
- **Processing pipeline** with visual step tracker: Upload → AI → Lucid → Done
- **Debug console** — activated via `?debug=true`, with real-time log streaming, color-coded stages, and resend buttons
- **Image record tracking** — full lifecycle with timestamps, AI provider/model, duration, shape/line counts, error tracking
- **Live status indicators** — real-time health checks for CB Lite, AI API, and Lucid REST API
- **Image history** — paginated table with thumbnails, status badges, and delete buttons
- **Light / Dark theme toggle** with localStorage persistence
- **Settings modal** — collapsible sections for all credentials and read-only timeout display
- **Single Docker container** deployment with Docker Compose
- **DaisyUI 5 + Tailwind CSS 4** frontend
