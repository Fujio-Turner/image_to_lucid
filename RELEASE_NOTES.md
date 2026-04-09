# Release Notes

## v1.1.0 — Image Optimization & User-Controlled Processing

### New Features

- **Image optimization** — All images are automatically downscaled (max 1500px) and re-encoded as JPEG (quality 85) before sending to AI APIs, reducing payload size and speeding up processing
- **Optimize Image toggle** — DaisyUI toggle next to the file input (on by default) lets users control whether optimization is applied
- **Image metadata preview** — After upload, displays original vs. optimized file size (KB/MB), dimensions, and estimated token count; updates in real-time when toggling optimization
- **Process Image button** — Replaces auto-processing; users can review metadata and settings before sending to the AI API
- **Delete button** — Lets users remove a mistaken upload before processing
- **Cancel button** — Process button converts to a red Cancel button during AI/Lucid API requests; uses `AbortController` to abort in-flight fetches
- **`/api/image-meta` endpoint** — Returns original and optimized image metadata (size, dimensions, estimated tokens) for the UI preview

### Improvements

- **Filename column overflow fix** — Recent Images table uses `table-fixed` layout with explicit column widths and `truncate` on filenames to prevent horizontal scrolling
- **Auto-chain preserved** — After clicking Process, the AI → Lucid pipeline still runs automatically; only the initial trigger requires user action
- **Process button restore** — On error or cancel, the Process + Delete buttons are automatically restored for retry

### Dependencies

- Added **Pillow** to `requirements.txt` for image resizing and re-encoding

### Tests

- 15 new unit tests (48 total) covering `_optimize_image`, `_encode_image_b64`, `/api/image-meta`, and optimize flag passthrough

---

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
