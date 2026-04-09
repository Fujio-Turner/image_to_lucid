# Release Notes

## v1.2.0 — AI Instructions & Full Lucid Standard Import Support

### New Features

- **AI Instructions text box** — Optional textarea on each upload card lets users send freeform instructions alongside the image to the AI (e.g., "Identify bottlenecks in this diagram", "Add a pipeline at step X connecting back to Y", "Remove the auth service and reconnect the flow"). The AI receives both the image and the user instructions in a single request.
- **Full Lucid Standard Import specification** — AI prompt rewritten to cover the complete Lucid Standard Import schema:
  - **Standard Library**: `rectangle`, `text`, `image`, `stickyNote`, `hotspot`
  - **Shape Library**: `circle`, `cloud`, `cross`, `diamond`, `doubleArrow`, `flexiblePolygon`, `hexagon`, `isoscelesTriangle`, `octagon`, `pentagon`, `polyStar`, `rightTriangle`, `singleArrow`
  - **Flowchart Library**: `process`, `decision`, `terminator`, `data`, `database`, `document`, `multipleDocuments`, `predefinedProcess`, `storedData`, `internalStorage`, `manualInput`, `manualOperation`, `preparation`, `display`, `delay`, `merge`, `connector`, `note`, `offPageLink`, `paperTape`, `directAccessStorage`, `or`, `summingJunction`, `braceNote`
  - **Container Library**: `rectangleContainer`, `roundedRectangleContainer`, `circleContainer`, `diamondContainer`, `pillContainer`, `braceContainer`, `bracketContainer`, `swimLanes`
  - **Table Library**: `table` with cells, merge, and row/column sizing
  - **22 endpoint styles**: `none`, `arrow`, `hollowArrow`, `openArrow`, `aggregation`, `composition`, `generalization`, `closedCircle`, `openCircle`, `closedSquare`, `openSquare`, `async1`, `async2`, `one`, `many`, `oneOrMore`, `zeroOrMore`, `zeroOrOne`, `exactlyOne`, `nesting`, `bpmnConditional`, `bpmnDefault`
  - **3 endpoint types**: `shapeEndpoint`, `positionEndpoint`, `lineEndpoint`
  - **Groups and Layers** support — AI can now output grouped shapes and layered diagrams, passed through to Lucid
  - **Stroke styles**: `solid`, `dashed`, `dotted`
  - **Fill types**: color and image URL with scale modes (`fit`, `fill`, `stretch`, `original`, `tile`)
- **Reference integrity validation (AI-side)** — Prompt includes a `CRITICAL — REFERENCE INTEGRITY` section instructing the AI to self-validate all shape/line ID references before returning JSON
- **Reference integrity validation (server-side)** — `_validate_references()` safety net drops lines with invalid endpoint references, filters group/layer items to valid IDs, and logs every dropped reference to the debug console
- **Token estimation breakdown** — `/api/image-meta` now returns `promptTokens` (schema prompt size) and per-section `imageTokens`; UI displays total with breakdown: `Est. tokens: ~1.2k (image ~218 + prompt ~1.0k)`

### Improvements

- **Groups & layers passthrough** — `_build_lucid_document()` now passes AI-generated `groups` and `layers` to the Lucid document instead of hardcoding empty arrays
- **AI chooses semantically appropriate shapes** — Prompt instructs AI to use flowchart types for flowcharts, containers for groupings, tables for tabular data, etc., instead of mapping everything to basic shapes
- **`/api/process` accepts `user_prompt`** — New optional field in the process request body, forwarded to all 4 AI providers (Gemini, OpenAI, Claude, xAI)

### Tests

- 21 new unit tests (48 → 69 total):
  - `TestBuildPrompt` (4) — prompt construction with/without user input
  - `TestValidateReferences` (10) — valid pass-through, invalid shape/line/group/layer refs, positionEndpoints, mixed valid/invalid, cascading drops, empty inputs
  - `TestBuildLucidDocumentGroupsLayers` (3) — groups/layers passthrough, defaults, invalid line stripping
  - `TestProcessImageUserPrompt` (2) — user prompt forwarded to AI provider, empty default
  - `TestImageMetaTokenBreakdown` (2) — promptTokens present, imageTokens + promptTokens = total

---

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
