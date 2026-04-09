import os
import sys
import io
import json
import time
import zipfile
import unittest
import tempfile
import shutil
from unittest.mock import patch, MagicMock

# Create temp directories and config before importing the app module
_test_dir = tempfile.mkdtemp()
_uploads_dir = os.path.join(_test_dir, "uploads")
_data_dir = os.path.join(_test_dir, "data")
_cbl_dir = os.path.join(_test_dir, "cbl")
os.makedirs(_uploads_dir, exist_ok=True)
os.makedirs(_data_dir, exist_ok=True)
os.makedirs(_cbl_dir, exist_ok=True)

_config = {
    "server": {"port": 8888, "debug": True},
    "timeouts": {"ai_api": 120, "lucid_api": 15},
    "ai_providers": {},
}
_config_path = os.path.join(_test_dir, "config.json")
with open(_config_path, "w") as f:
    json.dump(_config, f)

# Patch the module source so module-level constants point to temp dirs before import.
# We need to intercept open() for config AND patch os.makedirs targets.
_original_open = open

def _patched_open(path, *args, **kwargs):
    if path == "/app/config.json":
        return _original_open(_config_path, *args, **kwargs)
    return _original_open(path, *args, **kwargs)

# Pre-create the dirs that app.py will makedirs at import time
os.makedirs(os.path.join(_test_dir, "uploads"), exist_ok=True)
os.makedirs(os.path.join(_test_dir, "data"), exist_ok=True)

_original_makedirs = os.makedirs

def _patched_makedirs(path, *args, **kwargs):
    # Redirect /app/* paths to our temp dir
    if path.startswith("/app/"):
        path = os.path.join(_test_dir, path[5:])  # strip "/app/"
    return _original_makedirs(path, *args, **kwargs)

with patch("builtins.open", side_effect=_patched_open), \
     patch("os.makedirs", side_effect=_patched_makedirs):
    import app as app_module

# Override paths to use temp dirs
app_module.UPLOAD_FOLDER = _uploads_dir
app_module.CREDENTIALS_FILE = os.path.join(_data_dir, "credentials.json")
app_module.CBL_DB_DIR = _cbl_dir
app_module.app.config["UPLOAD_FOLDER"] = _uploads_dir


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app_module.app
        self.app.testing = True
        self.client = self.app.test_client()
        # Clear leftover data files
        cred_file = app_module.CREDENTIALS_FILE
        history_file = os.path.join(os.path.dirname(cred_file), "images.json")
        for f in [cred_file, history_file]:
            if os.path.exists(f):
                os.remove(f)
        # Clear debug log
        app_module._debug_log.clear()


class TestIndex(BaseTestCase):
    def test_index_returns_200(self):
        # Ensure static/index.html exists
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        os.makedirs(static_dir, exist_ok=True)
        index_path = os.path.join(static_dir, "index.html")
        created = False
        if not os.path.exists(index_path):
            with open(index_path, "w") as f:
                f.write("<html><body>test</body></html>")
            created = True
        try:
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 200)
        finally:
            if created:
                os.remove(index_path)


class TestUpload(BaseTestCase):
    def test_upload_success(self):
        data = {"file": (io.BytesIO(b"fake png data"), "test.png")}
        resp = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["filename"], "test.png")
        # File should exist on disk
        self.assertTrue(os.path.exists(os.path.join(_uploads_dir, "test.png")))

    def test_upload_no_file(self):
        resp = self.client.post("/upload", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_upload_empty_filename(self):
        data = {"file": (io.BytesIO(b"data"), "")}
        resp = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_upload_invalid_extension(self):
        data = {"file": (io.BytesIO(b"data"), "readme.txt")}
        resp = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not allowed", resp.get_json()["error"])

    def test_upload_creates_image_record(self):
        data = {"file": (io.BytesIO(b"fake png"), "record_test.png")}
        self.client.post("/upload", data=data, content_type="multipart/form-data")
        resp = self.client.get("/api/images")
        self.assertEqual(resp.status_code, 200)
        images = resp.get_json()["images"]
        filenames = [img["filename"] for img in images]
        self.assertIn("record_test.png", filenames)


class TestImages(BaseTestCase):
    def _upload(self, name):
        data = {"file": (io.BytesIO(b"fake"), name)}
        self.client.post("/upload", data=data, content_type="multipart/form-data")

    def test_get_images_empty(self):
        resp = self.client.get("/api/images")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["images"], [])
        self.assertEqual(body["total"], 0)

    def test_get_images_pagination(self):
        for i in range(5):
            self._upload(f"img{i}.png")
            time.sleep(0.01)  # ensure distinct timestamps
        resp = self.client.get("/api/images?limit=2&offset=0")
        body = resp.get_json()
        self.assertEqual(len(body["images"]), 2)
        self.assertEqual(body["total"], 5)
        resp2 = self.client.get("/api/images?limit=2&offset=2")
        body2 = resp2.get_json()
        self.assertEqual(len(body2["images"]), 2)

    def test_delete_image(self):
        self._upload("delete_me.png")
        resp = self.client.get("/api/images")
        images = resp.get_json()["images"]
        doc_id = images[0]["id"]
        del_resp = self.client.delete(f"/api/images/{doc_id}")
        self.assertEqual(del_resp.status_code, 200)
        # Verify gone
        resp2 = self.client.get("/api/images")
        self.assertEqual(resp2.get_json()["total"], 0)

    def test_delete_image_not_found(self):
        resp = self.client.delete("/api/images/nonexistent_id")
        self.assertEqual(resp.status_code, 404)


class TestCredentials(BaseTestCase):
    def test_get_default_credentials(self):
        resp = self.client.get("/api/credentials")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["selected_ai_provider"], "gemini")
        self.assertEqual(body["gemini"]["api_key"], "")

    def test_save_and_load_credentials(self):
        creds = {
            "selected_ai_provider": "openai",
            "lucid": {"api_key": "lucid-key"},
            "gemini": {"api_key": ""},
            "openai": {"api_key": "openai-key"},
            "claude": {"api_key": ""},
            "xai": {"api_key": ""},
        }
        resp = self.client.post("/api/credentials", json=creds)
        self.assertEqual(resp.status_code, 200)
        resp2 = self.client.get("/api/credentials")
        body = resp2.get_json()
        self.assertEqual(body["selected_ai_provider"], "openai")
        self.assertEqual(body["openai"]["api_key"], "openai-key")
        self.assertEqual(body["lucid"]["api_key"], "lucid-key")

    def test_credentials_sets_provider(self):
        creds = {
            "selected_ai_provider": "claude",
            "lucid": {"api_key": ""},
            "gemini": {"api_key": ""},
            "openai": {"api_key": ""},
            "claude": {"api_key": "claude-key"},
            "xai": {"api_key": ""},
        }
        self.client.post("/api/credentials", json=creds)
        body = self.client.get("/api/credentials").get_json()
        self.assertEqual(body["selected_ai_provider"], "claude")


class TestSettings(BaseTestCase):
    def test_get_settings(self):
        resp = self.client.get("/api/settings")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("server", body)
        self.assertIn("timeouts", body)
        self.assertEqual(body["server"]["port"], 8888)


class TestProcessImage(BaseTestCase):
    def _save_creds(self, provider="gemini", api_key="test-key"):
        creds = {
            "selected_ai_provider": provider,
            "lucid": {"api_key": ""},
            "gemini": {"api_key": api_key if provider == "gemini" else ""},
            "openai": {"api_key": api_key if provider == "openai" else ""},
            "claude": {"api_key": api_key if provider == "claude" else ""},
            "xai": {"api_key": api_key if provider == "xai" else ""},
        }
        self.client.post("/api/credentials", json=creds)

    def _create_dummy_file(self, name="test.png"):
        path = os.path.join(app_module.UPLOAD_FOLDER, name)
        with open(path, "wb") as f:
            f.write(b"fake png data")

    def test_process_no_filename(self):
        resp = self.client.post("/api/process", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("filename is required", resp.get_json()["error"])

    def test_process_file_not_found(self):
        resp = self.client.post("/api/process", json={"filename": "nonexistent.png"})
        self.assertEqual(resp.status_code, 404)

    def test_process_no_api_key(self):
        self._create_dummy_file()
        # Default credentials have empty api keys
        resp = self.client.post("/api/process", json={"filename": "test.png"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No API key", resp.get_json()["error"])

    def test_process_success(self):
        self._save_creds("gemini", "test-key")
        self._create_dummy_file()
        mock_result = {
            "title": "Test Diagram",
            "shapes": [{"id": "s1", "type": "rectangle", "text": "Hello"}],
            "lines": [{"id": "l1", "lineType": "straight"}],
        }
        with patch.dict(app_module.AI_PROVIDERS, {"gemini": MagicMock(return_value=mock_result)}):
            resp = self.client.post("/api/process", json={"filename": "test.png"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["provider"], "gemini")
        self.assertEqual(body["ai_result"]["title"], "Test Diagram")
        self.assertIn("lucid_document", body)
        self.assertEqual(body["lucid_document"]["version"], 1)

    def test_process_timeout(self):
        import requests as req_mod
        self._save_creds("gemini", "test-key")
        self._create_dummy_file()
        with patch.dict(app_module.AI_PROVIDERS, {"gemini": MagicMock(side_effect=req_mod.Timeout())}):
            resp = self.client.post("/api/process", json={"filename": "test.png"})
        self.assertEqual(resp.status_code, 504)
        self.assertIn("timed out", resp.get_json()["error"])

    def test_process_invalid_json(self):
        self._save_creds("gemini", "test-key")
        self._create_dummy_file()
        with patch.dict(app_module.AI_PROVIDERS, {
            "gemini": MagicMock(side_effect=json.JSONDecodeError("bad", "", 0))
        }):
            resp = self.client.post("/api/process", json={"filename": "test.png"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("invalid JSON", resp.get_json()["error"])

    def test_process_updates_image_record(self):
        self._save_creds("gemini", "test-key")
        self._create_dummy_file("update_test.png")
        # Upload so there's a record
        data = {"file": (io.BytesIO(b"fake png data"), "update_test.png")}
        self.client.post("/upload", data=data, content_type="multipart/form-data")
        mock_result = {
            "title": "Test",
            "shapes": [{"id": "s1", "type": "rectangle"}],
            "lines": [],
        }
        with patch.dict(app_module.AI_PROVIDERS, {"gemini": MagicMock(return_value=mock_result)}):
            self.client.post("/api/process", json={"filename": "update_test.png"})
        resp = self.client.get("/api/images")
        images = resp.get_json()["images"]
        rec = [i for i in images if i["filename"] == "update_test.png"][0]
        self.assertEqual(rec["status"], "ai_done")
        self.assertEqual(rec["ai_provider"], "gemini")
        self.assertEqual(rec["ai_model"], "gemini-2.0-flash")


class TestSendToLucid(BaseTestCase):
    def _save_creds_with_lucid_key(self, lucid_key="lucid-test-key"):
        creds = {
            "selected_ai_provider": "gemini",
            "lucid": {"api_key": lucid_key},
            "gemini": {"api_key": ""},
            "openai": {"api_key": ""},
            "claude": {"api_key": ""},
            "xai": {"api_key": ""},
        }
        self.client.post("/api/credentials", json=creds)

    def test_send_to_lucid_no_document(self):
        resp = self.client.post("/api/send-to-lucid", json={"title": "Test"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("lucid_document is required", resp.get_json()["error"])

    def test_send_to_lucid_no_key(self):
        resp = self.client.post("/api/send-to-lucid", json={
            "lucid_document": {"version": 1, "pages": []}
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No Lucid API key", resp.get_json()["error"])

    def test_send_to_lucid_success(self):
        self._save_creds_with_lucid_key()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"documentId": "123", "editUrl": "https://lucid.app/edit/123"}
        mock_resp.raise_for_status = MagicMock()
        with patch("app.requests.post", return_value=mock_resp):
            resp = self.client.post("/api/send-to-lucid", json={
                "lucid_document": {"version": 1, "pages": []},
                "title": "My Diagram",
            })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("lucid_response", body)
        self.assertEqual(body["lucid_response"]["documentId"], "123")

    def test_send_to_lucid_timeout(self):
        import requests as req_mod
        self._save_creds_with_lucid_key()
        with patch("app.requests.post", side_effect=req_mod.Timeout()):
            resp = self.client.post("/api/send-to-lucid", json={
                "lucid_document": {"version": 1, "pages": []},
            })
        self.assertEqual(resp.status_code, 504)
        self.assertIn("timed out", resp.get_json()["error"])


class TestDebugLog(BaseTestCase):
    def test_debug_log_empty(self):
        resp = self.client.get("/api/debug-log?since=0")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["entries"], [])

    def test_debug_log_entries_appear(self):
        app_module.debug_log("test-stage", "hello world", "some detail")
        resp = self.client.get("/api/debug-log?since=0")
        entries = resp.get_json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["stage"], "test-stage")
        self.assertEqual(entries[0]["message"], "hello world")
        self.assertEqual(entries[0]["detail"], "some detail")

    def test_debug_log_since_filter(self):
        app_module.debug_log("a", "first")
        time.sleep(0.05)
        cutoff = time.time()
        time.sleep(0.05)
        app_module.debug_log("b", "second")
        resp = self.client.get(f"/api/debug-log?since={cutoff}")
        entries = resp.get_json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["stage"], "b")

    def test_debug_log_clear(self):
        app_module.debug_log("x", "to be cleared")
        resp = self.client.delete("/api/debug-log")
        self.assertEqual(resp.status_code, 200)
        resp2 = self.client.get("/api/debug-log?since=0")
        self.assertEqual(resp2.get_json()["entries"], [])


class TestCblStats(BaseTestCase):
    def test_cbl_stats_fallback(self):
        resp = self.client.get("/api/cbl-stats")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["available"])


class TestBuildLucidDocument(BaseTestCase):
    def test_build_lucid_document(self):
        ai_result = {
            "title": "My Diagram",
            "shapes": [
                {"id": "s1", "type": "rectangle", "text": "Box A"},
                {"id": "s2", "type": "circle", "text": "Circle B"},
            ],
            "lines": [
                {"id": "l1", "lineType": "straight",
                 "endpoint1": {"type": "shapeEndpoint", "shapeId": "s1"},
                 "endpoint2": {"type": "shapeEndpoint", "shapeId": "s2"}},
            ],
        }
        doc = app_module._build_lucid_document(ai_result)
        self.assertEqual(doc["version"], 1)
        self.assertIsInstance(doc["pages"], list)
        self.assertEqual(len(doc["pages"]), 1)
        page = doc["pages"][0]
        self.assertEqual(page["title"], "My Diagram")
        self.assertEqual(len(page["shapes"]), 2)
        self.assertEqual(len(page["lines"]), 1)


class TestCreateLucidZip(BaseTestCase):
    def test_create_lucid_zip(self):
        doc_json = {"version": 1, "pages": [{"id": "p1", "title": "Test"}]}
        buf = app_module._create_lucid_zip(doc_json)
        self.assertIsInstance(buf, io.BytesIO)
        with zipfile.ZipFile(buf, "r") as zf:
            self.assertIn("document.json", zf.namelist())
            content = json.loads(zf.read("document.json"))
            self.assertEqual(content["version"], 1)
            self.assertEqual(content["pages"][0]["title"], "Test")


class TestHelpers(BaseTestCase):
    def test_allowed_file(self):
        self.assertTrue(app_module.allowed_file("photo.png"))
        self.assertTrue(app_module.allowed_file("photo.jpg"))
        self.assertTrue(app_module.allowed_file("photo.jpeg"))
        self.assertFalse(app_module.allowed_file("readme.txt"))
        self.assertFalse(app_module.allowed_file("doc.pdf"))
        self.assertFalse(app_module.allowed_file("anim.gif"))

    def test_get_mime_type(self):
        self.assertEqual(app_module._get_mime_type("img.png"), "image/png")
        self.assertEqual(app_module._get_mime_type("img.jpg"), "image/jpeg")
        self.assertEqual(app_module._get_mime_type("img.jpeg"), "image/jpeg")

    def test_get_model_name(self):
        self.assertEqual(app_module._get_model_name("gemini"), "gemini-2.0-flash")
        self.assertEqual(app_module._get_model_name("openai"), "gpt-4o")
        self.assertEqual(app_module._get_model_name("claude"), "claude-sonnet-4-20250514")
        self.assertEqual(app_module._get_model_name("xai"), "grok-4.20-reasoning")
        self.assertEqual(app_module._get_model_name("unknown"), "unknown")


def _create_test_png(path, width=200, height=100, color=(255, 0, 0)):
    """Create a real PNG file using Pillow for tests that need valid images."""
    from PIL import Image as PILImage
    img = PILImage.new("RGB", (width, height), color)
    img.save(path, format="PNG")


class TestOptimizeImage(BaseTestCase):
    def test_optimize_small_image(self):
        path = os.path.join(_uploads_dir, "small_opt.png")
        _create_test_png(path, 200, 100)
        original_size = os.path.getsize(path)
        optimized_bytes, was_optimized = app_module._optimize_image(path)
        self.assertTrue(was_optimized)
        self.assertIsInstance(optimized_bytes, bytes)
        self.assertGreater(len(optimized_bytes), 0)

    def test_optimize_large_image_downscales(self):
        path = os.path.join(_uploads_dir, "large_opt.png")
        _create_test_png(path, 3000, 2000)
        optimized_bytes, was_optimized = app_module._optimize_image(path)
        self.assertTrue(was_optimized)
        from PIL import Image as PILImage
        opt_img = PILImage.open(io.BytesIO(optimized_bytes))
        self.assertLessEqual(max(opt_img.size), app_module.IMAGE_OPTIMIZE_MAX_DIMENSION)

    def test_optimize_rgba_converts_to_rgb(self):
        from PIL import Image as PILImage
        path = os.path.join(_uploads_dir, "rgba_opt.png")
        img = PILImage.new("RGBA", (200, 100), (255, 0, 0, 128))
        img.save(path, format="PNG")
        optimized_bytes, was_optimized = app_module._optimize_image(path)
        self.assertTrue(was_optimized)
        opt_img = PILImage.open(io.BytesIO(optimized_bytes))
        self.assertEqual(opt_img.mode, "RGB")

    def test_optimize_within_max_dimension_no_resize(self):
        path = os.path.join(_uploads_dir, "norz_opt.png")
        _create_test_png(path, 800, 600)
        optimized_bytes, _ = app_module._optimize_image(path)
        from PIL import Image as PILImage
        opt_img = PILImage.open(io.BytesIO(optimized_bytes))
        self.assertEqual(opt_img.size, (800, 600))


class TestEncodeImageB64(BaseTestCase):
    def test_encode_with_optimize(self):
        path = os.path.join(_uploads_dir, "enc_opt.png")
        _create_test_png(path, 200, 100)
        b64, was_optimized = app_module._encode_image_b64(path, optimize=True)
        self.assertTrue(was_optimized)
        import base64
        decoded = base64.b64decode(b64)
        self.assertGreater(len(decoded), 0)

    def test_encode_without_optimize(self):
        path = os.path.join(_uploads_dir, "enc_noopt.png")
        _create_test_png(path, 200, 100)
        b64, was_optimized = app_module._encode_image_b64(path, optimize=False)
        self.assertFalse(was_optimized)
        import base64
        decoded = base64.b64decode(b64)
        with open(path, "rb") as f:
            self.assertEqual(decoded, f.read())


class TestImageMeta(BaseTestCase):
    def test_image_meta_no_filename(self):
        resp = self.client.post("/api/image-meta", json={})
        self.assertEqual(resp.status_code, 400)

    def test_image_meta_file_not_found(self):
        resp = self.client.post("/api/image-meta", json={"filename": "nope.png"})
        self.assertEqual(resp.status_code, 404)

    def test_image_meta_returns_original_and_optimized(self):
        path = os.path.join(_uploads_dir, "meta_test.png")
        _create_test_png(path, 3000, 2000)
        resp = self.client.post("/api/image-meta", json={"filename": "meta_test.png"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("original", body)
        self.assertIn("optimized", body)
        orig = body["original"]
        opt = body["optimized"]
        self.assertEqual(orig["width"], 3000)
        self.assertEqual(orig["height"], 2000)
        self.assertLessEqual(opt["width"], app_module.IMAGE_OPTIMIZE_MAX_DIMENSION)
        self.assertLessEqual(opt["height"], app_module.IMAGE_OPTIMIZE_MAX_DIMENSION)
        for key in ("size", "width", "height", "tokens"):
            self.assertIn(key, orig)
            self.assertIn(key, opt)

    def test_image_meta_small_image_still_has_optimized(self):
        path = os.path.join(_uploads_dir, "meta_small.png")
        _create_test_png(path, 200, 100)
        resp = self.client.post("/api/image-meta", json={"filename": "meta_small.png"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("optimized", body)
        self.assertEqual(body["optimized"]["width"], 200)
        self.assertEqual(body["optimized"]["height"], 100)


class TestProcessImageOptimizeFlag(BaseTestCase):
    def _save_creds(self, provider="gemini", api_key="test-key"):
        creds = {
            "selected_ai_provider": provider,
            "lucid": {"api_key": ""},
            "gemini": {"api_key": api_key if provider == "gemini" else ""},
            "openai": {"api_key": api_key if provider == "openai" else ""},
            "claude": {"api_key": api_key if provider == "claude" else ""},
            "xai": {"api_key": api_key if provider == "xai" else ""},
        }
        self.client.post("/api/credentials", json=creds)

    def test_process_passes_optimize_true(self):
        self._save_creds("gemini", "test-key")
        path = os.path.join(_uploads_dir, "opt_flag.png")
        _create_test_png(path, 200, 100)
        mock_result = {"title": "T", "shapes": [], "lines": []}
        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict(app_module.AI_PROVIDERS, {"gemini": mock_fn}):
            resp = self.client.post("/api/process", json={"filename": "opt_flag.png", "optimize": True})
        self.assertEqual(resp.status_code, 200)
        mock_fn.assert_called_once()
        self.assertTrue(mock_fn.call_args[0][4])  # 5th arg = optimize=True

    def test_process_passes_optimize_false(self):
        self._save_creds("gemini", "test-key")
        path = os.path.join(_uploads_dir, "opt_flag2.png")
        _create_test_png(path, 200, 100)
        mock_result = {"title": "T", "shapes": [], "lines": []}
        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict(app_module.AI_PROVIDERS, {"gemini": mock_fn}):
            resp = self.client.post("/api/process", json={"filename": "opt_flag2.png", "optimize": False})
        self.assertEqual(resp.status_code, 200)
        mock_fn.assert_called_once()
        self.assertFalse(mock_fn.call_args[0][4])  # 5th arg = optimize=False

    def test_process_defaults_optimize_true(self):
        self._save_creds("gemini", "test-key")
        path = os.path.join(_uploads_dir, "opt_default.png")
        _create_test_png(path, 200, 100)
        mock_result = {"title": "T", "shapes": [], "lines": []}
        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict(app_module.AI_PROVIDERS, {"gemini": mock_fn}):
            resp = self.client.post("/api/process", json={"filename": "opt_default.png"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(mock_fn.call_args[0][4])  # defaults to True


if __name__ == "__main__":
    unittest.main()
