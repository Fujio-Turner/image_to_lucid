import os
import io
import json
import time
import base64
import zipfile
import logging
import requests
from flask import Flask, request, jsonify, send_from_directory

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
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        debug_log("upload", f"File saved to disk: {filename}", filepath)
        _save_image_record(filename)
        return jsonify({"message": "File uploaded", "filename": filename}), 200
    debug_log("upload", "Upload error", "File type not allowed. Use png, jpeg, or jpg.")
    return jsonify({"error": "File type not allowed. Use png, jpeg, or jpg."}), 400


def _save_image_record(filename):
    ts = time.time()
    doc_id = f"img_{int(ts * 1000)}"
    debug_log("cbl-save", f"Saving image record: {filename}", f"doc_id={doc_id}, USE_CBL={USE_CBL}")
    if USE_CBL:
        db = _get_cbl_db()
        doc = MutableDocument(doc_id)
        doc["type"] = "image"
        doc["filename"] = filename
        doc["timestamp"] = ts
        doc["status"] = "uploaded"
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
        records.append({"id": doc_id, "filename": filename, "timestamp": ts, "status": "uploaded"})
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


LUCID_SCHEMA_PROMPT = """Analyze this image and extract all visual elements into a structured JSON object.
Return ONLY valid JSON with this exact schema — no markdown, no explanation:
{
  "title": "short descriptive title of the diagram",
  "shapes": [
    {
      "id": "shape1",
      "type": "rectangle",
      "text": "label text inside the shape",
      "boundingBox": {"x": 100, "y": 100, "w": 200, "h": 80},
      "style": {
        "fill": {"type": "color", "color": "#FFFFFF"},
        "stroke": {"color": "#333333", "width": 2, "style": "solid"},
        "rounding": 0
      }
    }
  ],
  "lines": [
    {
      "id": "line1",
      "lineType": "straight",
      "endpoint1": {"type": "shapeEndpoint", "style": "none", "shapeId": "shape1"},
      "endpoint2": {"type": "shapeEndpoint", "style": "arrow", "shapeId": "shape2"},
      "text": [{"text": "optional label", "position": 0.5, "side": "middle"}]
    }
  ]
}
Rules:
- Use ONLY these shape types: rectangle, circle, cloud, cross, diamond, doubleArrow, hexagon, isoscelesTriangle, octagon, pentagon, rightTriangle, singleArrow, text
- For rounded rectangles, use type "rectangle" with "rounding": 20 in the style.
- Position shapes on a grid starting at x=100,y=100. Space shapes ~250px apart horizontally, ~150px vertically.
- Every connection must reference existing shape IDs.
- lineType must be one of: straight, elbow, curved
- Line "text" MUST be an array of objects: [{"text":"label","position":0.5,"side":"middle"}]. Use empty array [] if no label.
- Do NOT include "position" in shapeEndpoint — omit it so Lucid auto-routes lines.
- Use hex colors from the image. Default fill #FFFFFF, stroke #333333.
- Extract ALL text visible in the image."""


def _encode_image_b64(filepath):
    with open(filepath, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _get_mime_type(filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")


def _call_gemini(api_key, filepath, filename, timeout):
    b64 = _encode_image_b64(filepath)
    mime = _get_mime_type(filename)
    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [
                {"text": LUCID_SCHEMA_PROMPT},
                {"inline_data": {"mime_type": mime, "data": b64}}
            ]}],
            "generationConfig": {"responseMimeType": "application/json"}
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _call_openai(api_key, filepath, filename, timeout):
    b64 = _encode_image_b64(filepath)
    mime = _get_mime_type(filename)
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": LUCID_SCHEMA_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]}],
            "max_tokens": 4096,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return json.loads(text)


def _call_claude(api_key, filepath, filename, timeout):
    b64 = _encode_image_b64(filepath)
    mime = _get_mime_type(filename)
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
                {"type": "text", "text": LUCID_SCHEMA_PROMPT}
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


def _call_xai(api_key, filepath, filename, timeout):
    b64 = _encode_image_b64(filepath)
    mime = _get_mime_type(filename)
    resp = requests.post(
        "https://api.x.ai/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "grok-4.20-reasoning",
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
                {"type": "input_text", "text": LUCID_SCHEMA_PROMPT},
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


def _build_lucid_document(ai_result):
    """Convert AI result into Lucid Standard Import document.json format."""
    return {
        "version": 1,
        "pages": [{
            "id": "page1",
            "title": ai_result.get("title", "AI Generated Diagram"),
            "shapes": ai_result.get("shapes", []),
            "lines": ai_result.get("lines", []),
            "groups": [],
            "layers": [],
        }],
    }


def _create_lucid_zip(document_json):
    """Create an in-memory .lucid ZIP file containing document.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("document.json", json.dumps(document_json, indent=2))
    buf.seek(0)
    return buf


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

    timeout = BUILD_CONFIG.get("timeouts", {}).get("ai_api", 30)
    debug_log("ai-request", f"Processing image: {filename}", f"provider={provider}")
    ai_sent_at = time.time()
    _update_image_record(filename, {
        "status": "ai_processing",
        "ai_provider": provider,
        "ai_model": _get_model_name(provider),
        "ai_sent_at": ai_sent_at,
    })

    try:
        debug_log("ai-request", f"Sending to {provider} API", f"filepath={filepath}, timeout={timeout}")
        ai_result = AI_PROVIDERS[provider](api_key, filepath, filename, timeout)
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
                "https://api.lucid.co/documents",
                headers={"Authorization": f"Bearer {lucid_key}", "Lucid-Api-Version": "1"},
                params={"limit": 1},
                timeout=min(lucid_timeout, 5),
            )
            lucid_ok = r.status_code not in (401, 403)
        except Exception:
            pass

    return jsonify({"cbl": cbl_ok, "ai": ai_ok, "lucid": lucid_ok})


if __name__ == "__main__":
    port = BUILD_CONFIG.get("server", {}).get("port", 8888)
    debug = BUILD_CONFIG.get("server", {}).get("debug", True)
    app.run(host="0.0.0.0", port=port, debug=debug)
