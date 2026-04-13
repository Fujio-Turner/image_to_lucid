import os
import io
import json
import time
import base64
import zipfile
import logging
import requests
from flask import Flask, request, jsonify, send_from_directory
from PIL import Image

from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("lucid")

try:
    from CouchbaseLite.Database import Database, DatabaseConfiguration
    from CouchbaseLite.Document import MutableDocument
    USE_CBL = True
except ImportError:
    USE_CBL = False

app = Flask(__name__, static_folder="static")

# Build/runtime config
BUILD_CONFIG_FILE = "/app/config.json"
with open(BUILD_CONFIG_FILE) as f:
    BUILD_CONFIG = json.load(f)

UPLOAD_FOLDER = "/app/uploads"
CBL_DB_DIR = "/app/data/lucid_db"
CBL_DB_NAME = "credentials_db"
CREDENTIALS_DOC_ID = "credentials"
CREDENTIALS_FILE = "/app/data/credentials.json"
ALLOWED_EXTENSIONS = {"png", "jpeg", "jpg"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CBL_DB_DIR, exist_ok=True)
os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# In-memory debug log for UI consumption
_debug_log = []
_DEBUG_LOG_MAX = 200

def debug_log(stage, message, detail=None):
    """Append a structured debug entry and also log to console."""
    import time as _t
    entry = {"ts": _t.time(), "stage": stage, "message": message}
    if detail is not None:
        # Truncate large details
        d = str(detail)
        if len(d) > 2000:
            d = d[:2000] + "…(truncated)"
        entry["detail"] = d
    _debug_log.append(entry)
    if len(_debug_log) > _DEBUG_LOG_MAX:
        _debug_log[:] = _debug_log[-_DEBUG_LOG_MAX:]
    logger.info(f"[{stage}] {message}" + (f" | {entry.get('detail','')[:200]}" if detail else ""))

DEFAULT_CREDENTIALS = {
    "selected_ai_provider": "gemini",
    "lucid": {"api_key": ""},
    "gemini": {"api_key": ""},
    "openai": {"api_key": ""},
    "claude": {"api_key": ""},
    "xai": {"api_key": ""},
}


def _get_cbl_db():
    config = DatabaseConfiguration(CBL_DB_DIR)
    return Database(CBL_DB_NAME, config)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def load_credentials(_caller=None):
    if _caller:
        debug_log("config", f"Loading credentials ({_caller})", f"USE_CBL={USE_CBL}")
    if USE_CBL:
        db = _get_cbl_db()
        doc = db.getDocument(CREDENTIALS_DOC_ID)
        if doc:
            data = doc.properties.get("data")
            if data:
                return json.loads(data)
        return dict(DEFAULT_CREDENTIALS)
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    return dict(DEFAULT_CREDENTIALS)


def save_credentials(cfg):
    debug_log("config", f"Saving credentials", f"USE_CBL={USE_CBL}, provider={cfg.get('selected_ai_provider','')}")
    if USE_CBL:
        db = _get_cbl_db()
        doc = db.getMutableDocument(CREDENTIALS_DOC_ID)
        if not doc:
            doc = MutableDocument(CREDENTIALS_DOC_ID)
        doc["data"] = json.dumps(cfg)
        db.saveDocument(doc)
        return
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/help.html")
def help_page():
    return send_from_directory("static", "help.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        debug_log("upload", "Upload error", "No file part")
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        debug_log("upload", "Upload error", "No selected file")
        return jsonify({"error": "No selected file"}), 400
    debug_log("upload", f"Upload request received: {file.filename if file else 'no file'}")
    if file and allowed_file(file.filename):
        original_filename = file.filename
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        debug_log("upload", f"File saved to disk: {filename}", filepath)
        _save_image_record(filename, original_filename)
        return jsonify({"message": "File uploaded", "filename": filename}), 200
    debug_log("upload", "Upload error", "File type not allowed. Use png, jpeg, or jpg.")
    return jsonify({"error": "File type not allowed. Use png, jpeg, or jpg."}), 400


def _save_image_record(filename, original_filename=""):
    ts = time.time()
    doc_id = f"img_{int(ts * 1000)}"
    debug_log("cbl-save", f"Saving image record: {filename}", f"doc_id={doc_id}, USE_CBL={USE_CBL}")
    if USE_CBL:
        db = _get_cbl_db()
        doc = MutableDocument(doc_id)
        doc["type"] = "image"
        doc["filename"] = filename
        doc["original_filename"] = original_filename or filename
        doc["timestamp"] = ts
        doc["status"] = "uploaded"
        doc["user_prompt"] = ""
        doc["optimize"] = True
        db.saveDocument(doc)
        # Update manifest with new ID
        ids = []
        m = db.getMutableDocument("image_manifest")
        if m:
            raw = m.get("ids")
            if raw:
                ids = json.loads(raw)
        else:
            m = MutableDocument("image_manifest")
        ids.append(doc_id)
        m["ids"] = json.dumps(ids)
        db.saveDocument(m)
        debug_log("cbl-save", f"Image record saved to Couchbase Lite", f"doc_id={doc_id}")
    else:
        history_file = os.path.join(os.path.dirname(CREDENTIALS_FILE), "images.json")
        records = []
        if os.path.exists(history_file):
            with open(history_file) as f:
                records = json.load(f)
        records.append({"id": doc_id, "filename": filename, "original_filename": original_filename or filename, "timestamp": ts, "status": "uploaded", "user_prompt": "", "optimize": True})
        with open(history_file, "w") as f:
            json.dump(records, f)
        debug_log("cbl-save", f"Image record saved to JSON file", f"doc_id={doc_id}")


def _update_image_record(filename, updates):
    """Update fields on the image record matching filename."""
    if USE_CBL:
        db = _get_cbl_db()
        manifest_doc = db.getDocument("image_manifest")
        if not manifest_doc:
            return
        all_ids = json.loads(manifest_doc.properties.get("ids", "[]"))
        for img_id in reversed(all_ids):
            doc = db.getDocument(img_id)
            if doc and doc.properties.get("filename") == filename:
                mdoc = db.getMutableDocument(img_id)
                for k, v in updates.items():
                    mdoc[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
                db.saveDocument(mdoc)
                debug_log("cbl-save", f"Updated image record: {img_id}", f"fields={list(updates.keys())}")
                return
    else:
        history_file = os.path.join(os.path.dirname(CREDENTIALS_FILE), "images.json")
        if not os.path.exists(history_file):
            return
        with open(history_file) as f:
            records = json.load(f)
        for rec in reversed(records):
            if rec.get("filename") == filename:
                rec.update(updates)
                break
        with open(history_file, "w") as f:
            json.dump(records, f)


@app.route("/api/images")
def get_images():
    limit = request.args.get("limit", 10, type=int)
    offset = request.args.get("offset", 0, type=int)
    if USE_CBL:
        db = _get_cbl_db()
        # CBL-Python doesn't support N1QL; scan all image docs via known IDs
        # We store a manifest doc to track image IDs
        manifest_doc = db.getDocument("image_manifest")
        all_ids = []
        if manifest_doc:
            raw = manifest_doc.properties.get("ids")
            if raw:
                all_ids = json.loads(raw)
        total = len(all_ids)
        # Sort by newest first (IDs contain timestamp)
        all_ids.sort(reverse=True)
        page_ids = all_ids[offset:offset + limit]
        images = []
        for img_id in page_ids:
            doc = db.getDocument(img_id)
            if doc:
                props = doc.properties
                images.append({
                    "id": img_id,
                    "filename": props.get("filename", ""),
                    "original_filename": props.get("original_filename", props.get("filename", "")),
                    "timestamp": props.get("timestamp", 0),
                    "status": props.get("status", ""),
                    "ai_provider": props.get("ai_provider", ""),
                    "ai_model": props.get("ai_model", ""),
                    "ai_sent_at": props.get("ai_sent_at", 0),
                    "ai_received_at": props.get("ai_received_at", 0),
                    "ai_duration_s": props.get("ai_duration_s", 0),
                    "ai_shapes": props.get("ai_shapes", 0),
                    "ai_lines": props.get("ai_lines", 0),
                    "lucid_sent_at": props.get("lucid_sent_at", 0),
                    "lucid_received_at": props.get("lucid_received_at", 0),
                    "lucid_duration_s": props.get("lucid_duration_s", 0),
                    "error_source": props.get("error_source", ""),
                    "error_detail": props.get("error_detail", ""),
                    "user_prompt": props.get("user_prompt", ""),
                    "optimize": props.get("optimize", True),
                    "lucid_response": props.get("lucid_response", ""),
                })
        return jsonify({"images": images, "total": total})
    else:
        history_file = os.path.join(os.path.dirname(CREDENTIALS_FILE), "images.json")
        records = []
        if os.path.exists(history_file):
            with open(history_file) as f:
                records = json.load(f)
        records.sort(key=lambda r: r["timestamp"], reverse=True)
        total = len(records)
        page = records[offset:offset + limit]
        return jsonify({"images": page, "total": total})


@app.route("/api/images/<doc_id>", methods=["DELETE"])
def delete_image(doc_id):
    if USE_CBL:
        db = _get_cbl_db()
        doc = db.getDocument(doc_id)
        if not doc:
            return jsonify({"error": "Image not found"}), 404
        filename = doc.properties.get("filename", "")
        db.purgeDocument(doc_id)
        # Remove from manifest
        m = db.getMutableDocument("image_manifest")
        if m:
            raw = m.get("ids")
            if raw:
                ids = json.loads(raw)
                ids = [i for i in ids if i != doc_id]
                m["ids"] = json.dumps(ids)
                db.saveDocument(m)
    else:
        history_file = os.path.join(os.path.dirname(CREDENTIALS_FILE), "images.json")
        records = []
        if os.path.exists(history_file):
            with open(history_file) as f:
                records = json.load(f)
        found = [r for r in records if r["id"] == doc_id]
        if not found:
            return jsonify({"error": "Image not found"}), 404
        filename = found[0].get("filename", "")
        records = [r for r in records if r["id"] != doc_id]
        with open(history_file, "w") as f:
            json.dump(records, f)
    # Delete file from disk
    if filename:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
    debug_log("delete", f"Deleted image record: {doc_id}", f"filename={filename}")
    return jsonify({"message": "Deleted"})


@app.route("/api/settings", methods=["GET"])
def get_build_config():
    return jsonify(BUILD_CONFIG)


@app.route("/api/credentials", methods=["GET"])
def get_credentials():
    return jsonify(load_credentials())


@app.route("/api/credentials", methods=["POST"])
def update_credentials():
    cfg = request.get_json()
    save_credentials(cfg)
    return jsonify({"message": "Credentials saved"})


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


LUCID_SCHEMA_PROMPT = """Analyze this image and extract all visual elements into a structured JSON object compatible with Lucid Standard Import.
Return ONLY valid JSON with this exact top-level schema — no markdown, no explanation:
{
  "title": "short descriptive title of the diagram",
  "shapes": [ ... ],
  "lines": [ ... ],
  "groups": [ ... ],
  "layers": [ ... ]
}

=== SHAPES ===
Each shape object requires: id (unique string), type, and boundingBox.
Optional properties: style, text, opacity (0-100, default 100), note, zIndex, customData, actions.

boundingBox format: {"x": 100, "y": 100, "w": 200, "h": 80, "rotation": 0}
  - rotation is optional (0-360 degrees clockwise).

style format:
{
  "fill": {"type": "color", "color": "#FFFFFF"},
  "stroke": {"color": "#333333", "width": 2, "style": "solid"},
  "rounding": 0,
  "textColor": "#000000"
}
  - fill can also be {"type": "image", "url": "https://..."} with optional "imageScale": "fit"|"fill"|"stretch"|"original"|"tile".
  - stroke.style: "solid", "dashed", or "dotted".
  - rounding: integer, double the corner radius in pixels.

Allowed shape types by library:

Standard Library: rectangle, text, image, stickyNote, hotspot
Shape Library: circle, cloud, cross, diamond, doubleArrow, flexiblePolygon, hexagon, isoscelesTriangle, octagon, pentagon, polyStar, rightTriangle, singleArrow
  - singleArrow has optional "orientation": "right"|"left"|"up"|"down".
  - cross has optional "indent": {"x": 0.25, "y": 0.25}.
  - polyStar requires "shape": {"numPoints": 5, "innerRadius": 0.5}.
  - flexiblePolygon requires "vertices": [{"x":0,"y":0}, {"x":1,"y":0}, {"x":0.5,"y":1}] (3-100 relative positions).

Flowchart Library: process, decision, terminator, data, database, document, multipleDocuments, predefinedProcess, storedData, internalStorage, manualInput, manualOperation, preparation, display, delay, merge, connector, note, offPageLink, paperTape, directAccessStorage, or, summingJunction, braceNote
  - predefinedProcess requires "sideWidth": 0.1 (0 to 0.33).
  - braceNote requires "rightFacing": true/false and "braceWidth": 60.
  - "or" and "summingJunction" do NOT accept text.

Container Library: rectangleContainer, roundedRectangleContainer, circleContainer, diamondContainer, pillContainer, braceContainer, bracketContainer, swimLanes
  - Containers have optional "magnetize": true, "containerTitle": {"text": "Title"}, "assistedLayout": true.
  - swimLanes requires "vertical": bool, "titleBar": {"height": 50, "verticalText": true}, "lanes": [{"title": "Lane 1", "width": 300, "headerFill": "#E0E0E0", "laneFill": "#FFFFFF"}].

Table Library: table
  - Requires "rowCount", "colCount", "cells": [{"xPosition": 0, "yPosition": 0, "text": "Cell text", "style": {"fill": {"type":"color","color":"#FFF"}}, "mergeCellsRight": 0, "mergeCellsDown": 0}].
  - Optional "userSpecifiedRows": [{"index": 0, "size": 40}], "userSpecifiedCols": [{"index": 0, "size": 160}].

=== LINES ===
Each line requires: id, lineType, endpoint1, endpoint2.
Optional: stroke, text, customData, joints, elbowControlPoints, zIndex.

lineType: "straight", "elbow", or "curved".

Endpoint types:
  - shapeEndpoint: {"type": "shapeEndpoint", "style": "arrow", "shapeId": "shape1"} — omit "position" for auto-routing (smart lines).
  - positionEndpoint: {"type": "positionEndpoint", "style": "none", "position": {"x": 100, "y": 200}} — for free-floating line ends.
  - lineEndpoint: {"type": "lineEndpoint", "style": "arrow", "lineId": "line2", "position": 0.5} — attach to another line.

Endpoint styles: none, arrow, hollowArrow, openArrow, aggregation, composition, generalization, closedCircle, openCircle, closedSquare, openSquare, async1, async2, one, many, oneOrMore, zeroOrMore, zeroOrOne, exactlyOne, nesting, bpmnConditional, bpmnDefault.

Line text MUST be an array: [{"text": "label", "position": 0.5, "side": "middle"}]. Use [] if no label.
  - side: "top", "middle", or "bottom".
  - position: 0.0 to 1.0 (relative position along the line).

=== GROUPS ===
{"id": "group1", "items": ["shape1", "shape2", "line1"], "zIndex": 0}
  - items: array of shape, line, or other group IDs.

=== LAYERS ===
{"id": "layer1", "title": "Background", "items": ["shape1", "line1"], "layerIndex": 0}

=== RULES ===
- Choose the most semantically appropriate shape type for what the image depicts. Use flowchart types for flowcharts, containers for groupings, tables for tabular data, etc.
- For rounded rectangles, use type "rectangle" with "rounding": 20 in the style.
- Position shapes on a grid starting at x=100, y=100. Space shapes ~250px apart horizontally, ~150px vertically.
- Use hex colors (#RRGGBB) from the image. Default fill #FFFFFF, stroke #333333.
- Do NOT include "position" in shapeEndpoint — omit it so Lucid auto-routes lines.
- Extract ALL text visible in the image.
- Use groups to logically cluster related shapes.
- Only include groups and layers arrays if appropriate for the diagram; otherwise use empty arrays.

=== CRITICAL — REFERENCE INTEGRITY ===
Before returning your JSON, you MUST self-validate:
1. Build a list of every shape "id" you defined in the "shapes" array.
2. Build a list of every line "id" you defined in the "lines" array.
3. For EVERY line endpoint, check:
   - If type is "shapeEndpoint", the "shapeId" MUST match an id from your shapes list.
   - If type is "lineEndpoint", the "lineId" MUST match an id from your lines list.
4. For EVERY group, every item in "items" MUST match a shape, line, or group id you defined.
5. For EVERY layer, every item in "items" MUST match a shape, line, or group id you defined.
6. If any reference does not match, fix it — either correct the id to the right one, or remove the line/item.
7. Do NOT invent or hallucinate IDs. Only use IDs you have explicitly defined in shapes or lines.
Count your shapes and lines, then count the distinct IDs referenced by endpoints — they must all resolve."""


IMAGE_OPTIMIZE_MAX_DIMENSION = 1500


def _optimize_image(filepath):
    """Downscale and re-encode image as JPEG. Returns (bytes, True)."""
    file_size = os.path.getsize(filepath)
    img = Image.open(filepath)
    w, h = img.size
    max_dim = IMAGE_OPTIMIZE_MAX_DIMENSION
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        debug_log("optimize", f"Resized {w}x{h} → {new_w}x{new_h}", f"original={file_size} bytes")
    else:
        debug_log("optimize", f"Re-encoding at {w}x{h} (within max dimension)", f"original={file_size} bytes")

    if img.mode == "RGBA":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    optimized = buf.getvalue()
    debug_log("optimize", f"Optimized size: {len(optimized)} bytes", f"reduction={round((1 - len(optimized)/file_size)*100)}%")
    return optimized, True


def _encode_image_b64(filepath, optimize=True):
    if optimize:
        img_bytes, was_optimized = _optimize_image(filepath)
        return base64.b64encode(img_bytes).decode("utf-8"), was_optimized
    with open(filepath, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8"), False


def _get_mime_type(filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")


def _build_prompt(user_prompt=""):
    """Build the full prompt, appending user instructions if provided."""
    if user_prompt:
        return LUCID_SCHEMA_PROMPT + "\n\nAdditional user instructions:\n" + user_prompt
    return LUCID_SCHEMA_PROMPT


def _call_gemini(api_key, filepath, filename, timeout, optimize=True, user_prompt=""):
    b64, was_optimized = _encode_image_b64(filepath, optimize)
    mime = "image/jpeg" if was_optimized else _get_mime_type(filename)
    prompt = _build_prompt(user_prompt)
    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": b64}}
            ]}],
            "generationConfig": {"responseMimeType": "application/json"}
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _call_openai(api_key, filepath, filename, timeout, optimize=True, user_prompt=""):
    b64, was_optimized = _encode_image_b64(filepath, optimize)
    mime = "image/jpeg" if was_optimized else _get_mime_type(filename)
    prompt = _build_prompt(user_prompt)
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]}],
            "max_tokens": 4096,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return json.loads(text)


def _call_claude(api_key, filepath, filename, timeout, optimize=True, user_prompt=""):
    b64, was_optimized = _encode_image_b64(filepath, optimize)
    mime = "image/jpeg" if was_optimized else _get_mime_type(filename)
    prompt = _build_prompt(user_prompt)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt}
            ]}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def _call_xai(api_key, filepath, filename, timeout, optimize=True, user_prompt=""):
    b64, was_optimized = _encode_image_b64(filepath, optimize)
    mime = "image/jpeg" if was_optimized else _get_mime_type(filename)
    prompt = _build_prompt(user_prompt)
    resp = requests.post(
        "https://api.x.ai/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "grok-4.20-reasoning",
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
                {"type": "input_text", "text": prompt},
            ]}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text += part.get("text", "")
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


AI_MODEL_NAMES = {
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o",
    "claude": "claude-sonnet-4-20250514",
    "xai": "grok-4.20-reasoning",
}

def _get_model_name(provider):
    return AI_MODEL_NAMES.get(provider, provider)


AI_PROVIDERS = {
    "gemini": _call_gemini,
    "openai": _call_openai,
    "claude": _call_claude,
    "xai": _call_xai,
}


def _normalize_color(value):
    """Ensure color is a valid 7-char hex string (#RRGGBB). Strip alpha, fix shorthand."""
    if not isinstance(value, str):
        return "#FFFFFF"
    value = value.strip()
    if not value.startswith("#"):
        value = "#" + value
    hex_part = value[1:]
    hex_part = "".join(c for c in hex_part if c in "0123456789abcdefABCDEF")
    if len(hex_part) == 3:
        hex_part = "".join(c * 2 for c in hex_part)
    if len(hex_part) == 8:
        hex_part = hex_part[:6]
    if len(hex_part) != 6:
        return "#FFFFFF"
    return "#" + hex_part.upper()


def _sanitize_shapes(shapes):
    """Sanitize color values and bounding boxes in shapes so Lucid accepts them."""
    for shape in shapes:
        bb = shape.get("boundingBox", {})
        if "rotation" in bb:
            bb["rotation"] = max(0, min(360, bb["rotation"]))
        style = shape.get("style", {})
        fill = style.get("fill", {})
        if "color" in fill:
            fill["color"] = _normalize_color(fill["color"])
        stroke = style.get("stroke", {})
        if "color" in stroke:
            stroke["color"] = _normalize_color(stroke["color"])
    return shapes


def _validate_references(shapes, lines, groups, layers):
    """Remove lines/groups/layers that reference non-existent IDs."""
    shape_ids = {s["id"] for s in shapes if "id" in s}
    line_ids = {l["id"] for l in lines if "id" in l}
    all_ids = shape_ids | line_ids

    def _endpoint_valid(ep):
        if not ep or not ep.get("type"):
            return False
        ep_type = ep["type"]
        if ep_type == "shapeEndpoint":
            return ep.get("shapeId") in shape_ids
        if ep_type == "lineEndpoint":
            return ep.get("lineId") in line_ids
        if ep_type == "positionEndpoint":
            return "x" in ep and "y" in ep
        return False

    valid_lines = []
    for line in lines:
        ep1 = line.get("endpoint1")
        ep2 = line.get("endpoint2")
        if ep1 and ep2 and _endpoint_valid(ep1) and _endpoint_valid(ep2):
            valid_lines.append(line)
        else:
            ep1_info = ep1.get('shapeId', ep1.get('lineId', ep1.get('type', ''))) if ep1 else 'missing'
            ep2_info = ep2.get('shapeId', ep2.get('lineId', ep2.get('type', ''))) if ep2 else 'missing'
            debug_log("validate", f"Dropped line with invalid/missing endpoints: {line.get('id','?')}",
                      f"ep1={ep1_info}, ep2={ep2_info}")

    # Recalculate valid IDs after dropping bad lines
    valid_line_ids = {l["id"] for l in valid_lines if "id" in l}
    all_valid_ids = shape_ids | valid_line_ids

    valid_groups = []
    for group in groups:
        items = group.get("items", [])
        filtered = [i for i in items if i in all_valid_ids]
        if filtered:
            group["items"] = filtered
            valid_groups.append(group)
            all_valid_ids.add(group["id"])

    valid_layers = []
    for layer in layers:
        items = layer.get("items", [])
        filtered = [i for i in items if i in all_valid_ids]
        if filtered:
            layer["items"] = filtered
            valid_layers.append(layer)

    dropped = len(lines) - len(valid_lines)
    if dropped:
        debug_log("validate", f"Dropped {dropped} line(s) with invalid references")

    return valid_lines, valid_groups, valid_layers


def _build_lucid_document(ai_result):
    """Convert AI result into Lucid Standard Import document.json format."""
    shapes = _sanitize_shapes(ai_result.get("shapes", []))
    lines = ai_result.get("lines", [])
    groups = ai_result.get("groups", [])
    layers = ai_result.get("layers", [])
    lines, groups, layers = _validate_references(shapes, lines, groups, layers)
    return {
        "version": 1,
        "pages": [{
            "id": "page1",
            "title": ai_result.get("title", "AI Generated Diagram"),
            "shapes": shapes,
            "lines": lines,
            "groups": groups,
            "layers": layers,
        }],
    }


def _create_lucid_zip(document_json):
    """Create an in-memory .lucid ZIP file containing document.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("document.json", json.dumps(document_json, indent=2))
    buf.seek(0)
    return buf


@app.route("/api/image-meta", methods=["POST"])
def image_meta():
    """Return original and optimized metadata for an uploaded image."""
    data = request.get_json()
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "filename is required"}), 400

    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    original_size = os.path.getsize(filepath)
    img = Image.open(filepath)
    orig_w, orig_h = img.size

    # Estimate token count: ~1 token per 4 chars for text, ~1 per 750 bytes of base64 for images
    prompt_tokens = len(LUCID_SCHEMA_PROMPT) // 4
    original_b64_size = (original_size * 4) // 3
    original_image_tokens = original_b64_size // 750
    original_tokens = original_image_tokens + prompt_tokens

    result = {
        "promptTokens": prompt_tokens,
        "original": {
            "size": original_size,
            "width": orig_w,
            "height": orig_h,
            "imageTokens": original_image_tokens,
            "tokens": original_tokens,
        },
    }

    # Always compute optimized preview so the UI can show the difference
    opt_img = img.copy()
    opt_w, opt_h = orig_w, orig_h
    max_dim = IMAGE_OPTIMIZE_MAX_DIMENSION
    if max(orig_w, orig_h) > max_dim:
        scale = max_dim / max(orig_w, orig_h)
        opt_w, opt_h = int(orig_w * scale), int(orig_h * scale)
        opt_img = opt_img.resize((opt_w, opt_h), Image.LANCZOS)
    if opt_img.mode == "RGBA":
        opt_img = opt_img.convert("RGB")
    buf = io.BytesIO()
    opt_img.save(buf, format="JPEG", quality=85)
    opt_size = buf.tell()
    opt_b64_size = (opt_size * 4) // 3
    opt_image_tokens = opt_b64_size // 750
    opt_tokens = opt_image_tokens + prompt_tokens
    result["optimized"] = {
        "size": opt_size,
        "width": opt_w,
        "height": opt_h,
        "imageTokens": opt_image_tokens,
        "tokens": opt_tokens,
    }

    return jsonify(result)


@app.route("/api/process", methods=["POST"])
def process_image():
    """Send an uploaded image to the selected AI provider and return Lucid-ready JSON."""
    data = request.get_json()
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "filename is required"}), 400

    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    creds = load_credentials("process_image")
    provider = creds.get("selected_ai_provider", "")
    if provider not in AI_PROVIDERS:
        return jsonify({"error": f"Unknown AI provider: {provider}"}), 400

    api_key = creds.get(provider, {}).get("api_key", "")
    if not api_key:
        return jsonify({"error": f"No API key configured for {provider}"}), 400

    optimize = data.get("optimize", True)
    user_prompt = data.get("user_prompt", "").strip()
    timeout = BUILD_CONFIG.get("timeouts", {}).get("ai_api", 30)
    debug_log("ai-request", f"Processing image: {filename}", f"provider={provider}, optimize={optimize}, user_prompt={'yes' if user_prompt else 'no'}")
    ai_sent_at = time.time()
    _update_image_record(filename, {
        "status": "ai_processing",
        "ai_provider": provider,
        "ai_model": _get_model_name(provider),
        "ai_sent_at": ai_sent_at,
        "user_prompt": user_prompt,
        "optimize": optimize,
    })

    try:
        debug_log("ai-request", f"Sending to {provider} API", f"filepath={filepath}, timeout={timeout}, optimize={optimize}")
        ai_result = AI_PROVIDERS[provider](api_key, filepath, filename, timeout, optimize, user_prompt)
        debug_log("ai-response", f"AI response received from {provider}", f"shapes={len(ai_result.get('shapes',[]))}, lines={len(ai_result.get('lines',[]))}")
        ai_received_at = time.time()
        _update_image_record(filename, {
            "status": "ai_done",
            "ai_received_at": ai_received_at,
            "ai_duration_s": round(ai_received_at - ai_sent_at, 2),
            "ai_shapes": len(ai_result.get("shapes", [])),
            "ai_lines": len(ai_result.get("lines", [])),
        })
    except requests.Timeout:
        debug_log("ai-error", f"{provider} request timed out", f"timeout={timeout}s")
        _update_image_record(filename, {"status": "error", "error_source": "ai", "error_detail": f"{provider} request timed out"})
        return jsonify({"error": f"{provider} request timed out"}), 504
    except json.JSONDecodeError as e:
        debug_log("ai-error", f"AI returned invalid JSON", str(e))
        _update_image_record(filename, {"status": "error", "error_source": "ai", "error_detail": f"AI returned invalid JSON: {str(e)}"})
        return jsonify({"error": f"AI returned invalid JSON: {str(e)}"}), 502
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        debug_log("ai-error", f"{provider} HTTP {e.response.status_code}", f"{str(e)} | response_body={body}")
        _update_image_record(filename, {"status": "error", "error_source": "ai", "error_detail": f"{provider} HTTP {e.response.status_code}"})
        return jsonify({"error": f"{provider} error: {str(e)}", "detail": body}), 502
    except Exception as e:
        debug_log("ai-error", f"{provider} error", str(e))
        _update_image_record(filename, {"status": "error", "error_source": "ai", "error_detail": f"{provider} error: {str(e)}"})
        return jsonify({"error": f"{provider} error: {str(e)}"}), 502

    lucid_doc = _build_lucid_document(ai_result)
    debug_log("ai-response", f"Lucid document built", f"title={ai_result.get('title','')}")
    return jsonify({
        "provider": provider,
        "ai_result": ai_result,
        "lucid_document": lucid_doc,
    })


@app.route("/api/send-to-lucid", methods=["POST"])
def send_to_lucid():
    """Create a Lucid document via Standard Import from the AI result."""
    data = request.get_json()
    lucid_doc = data.get("lucid_document")
    title = data.get("title", "AI Generated Diagram")
    filename = data.get("filename", "")
    if not lucid_doc:
        return jsonify({"error": "lucid_document is required"}), 400

    creds = load_credentials("send_to_lucid")
    lucid_key = creds.get("lucid", {}).get("api_key", "")
    if not lucid_key:
        return jsonify({"error": "No Lucid API key configured"}), 400

    timeout = BUILD_CONFIG.get("timeouts", {}).get("lucid_api", 15)
    lucid_zip = _create_lucid_zip(lucid_doc)
    debug_log("lucid-request", f"Sending document to Lucid API", f"title={title}")
    lucid_sent_at = time.time()
    if filename:
        _update_image_record(filename, {
            "status": "lucid_sending",
            "lucid_sent_at": lucid_sent_at,
        })

    try:
        resp = requests.post(
            "https://api.lucid.co/documents",
            headers={
                "Authorization": f"Bearer {lucid_key}",
                "Lucid-Api-Version": "1",
            },
            data={"title": title, "product": "lucidchart"},
            files={"file": ("import.lucid", lucid_zip, "x-application/vnd.lucid.standardImport")},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.Timeout:
        debug_log("lucid-error", f"Lucid API error", "Request timed out")
        if filename:
            _update_image_record(filename, {"status": "error", "error_source": "lucid", "error_detail": "Lucid API request timed out"})
        return jsonify({"error": "Lucid API request timed out"}), 504
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        debug_log("lucid-error", f"Lucid API error", str(detail))
        if filename:
            _update_image_record(filename, {"status": "error", "error_source": "lucid", "error_detail": f"Lucid HTTP {e.response.status_code}"})
        return jsonify({"error": "Lucid API error", "detail": detail}), e.response.status_code
    except Exception as e:
        debug_log("lucid-error", f"Lucid API error", str(e))
        if filename:
            _update_image_record(filename, {"status": "error", "error_source": "lucid", "error_detail": f"Lucid API error: {str(e)}"})
        return jsonify({"error": f"Lucid API error: {str(e)}"}), 502

    lucid_received_at = time.time()
    if filename:
        _update_image_record(filename, {
            "status": "done",
            "lucid_received_at": lucid_received_at,
            "lucid_duration_s": round(lucid_received_at - lucid_sent_at, 2),
            "lucid_response": resp.json(),
        })
    debug_log("lucid-response", f"Lucid document created", str(resp.json()))
    return jsonify({"message": "Document created in Lucid", "lucid_response": resp.json()})


@app.route("/api/debug-log")
def get_debug_log():
    since = request.args.get("since", 0, type=float)
    entries = [e for e in _debug_log if e["ts"] > since]
    return jsonify({"entries": entries})

@app.route("/api/debug-log", methods=["DELETE"])
def clear_debug_log():
    _debug_log.clear()
    return jsonify({"message": "Debug log cleared"})


@app.route("/api/cbl-stats")
def get_cbl_stats():
    stats = {"available": USE_CBL, "db_name": CBL_DB_NAME, "db_dir": CBL_DB_DIR, "doc_count": 0}
    if USE_CBL:
        try:
            db = _get_cbl_db()
            stats["doc_count"] = db.count
        except Exception as e:
            stats["error"] = str(e)
    else:
        # Fallback: count docs from JSON files
        cred_exists = os.path.exists(CREDENTIALS_FILE)
        history_file = os.path.join(os.path.dirname(CREDENTIALS_FILE), "images.json")
        img_count = 0
        if os.path.exists(history_file):
            with open(history_file) as f:
                img_count = len(json.load(f))
        stats["doc_count"] = (1 if cred_exists else 0) + img_count
    return jsonify(stats)


@app.route("/api/status")
def get_status():
    # Couchbase Lite status
    cbl_ok = False
    if USE_CBL:
        try:
            db = _get_cbl_db()
            cbl_ok = db is not None
        except Exception:
            pass

    # AI API status — ping the selected provider
    ai_ok = False
    creds = load_credentials()
    ai_timeout = BUILD_CONFIG.get("timeouts", {}).get("ai_api", 5)
    provider_urls = {
        "openai": "https://api.openai.com",
        "gemini": "https://generativelanguage.googleapis.com",
        "claude": "https://api.anthropic.com",
        "xai": "https://api.x.ai",
    }
    selected = creds.get("selected_ai_provider", "")
    if selected and selected in provider_urls:
        key = creds.get(selected, {}).get("api_key", "")
        if key:
            try:
                r = requests.get(provider_urls[selected], timeout=min(ai_timeout, 5))
                ai_ok = r.status_code < 500
            except Exception:
                pass

    # Lucid REST API status
    lucid_ok = False
    lucid_timeout = BUILD_CONFIG.get("timeouts", {}).get("lucid_api", 5)
    lucid_key = creds.get("lucid", {}).get("api_key", "")
    if lucid_key:
        try:
            r = requests.get(
                "https://api.lucid.co/users",
                headers={"Authorization": f"Bearer {lucid_key}", "Lucid-Api-Version": "1"},
                timeout=min(lucid_timeout, 5),
            )
            lucid_ok = r.status_code not in (401, 500, 502, 503)
        except Exception:
            pass

    return jsonify({"cbl": cbl_ok, "ai": ai_ok, "lucid": lucid_ok})


if __name__ == "__main__":
    port = BUILD_CONFIG.get("server", {}).get("port", 8888)
    debug = BUILD_CONFIG.get("server", {}).get("debug", True)
    app.run(host="0.0.0.0", port=port, debug=debug)
