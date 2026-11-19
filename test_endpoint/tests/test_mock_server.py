"""Offline contract/cache tests plus an unchanged-client HTTP smoke test."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

ENDPOINT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENDPOINT_DIR))
import mock_server as mock


def make_image(path: Path, image_format="PNG"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), (120, 80, 40)).save(path, format=image_format)
    return path


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-api-", dir=ENDPOINT_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 100.0
        self.images = mock.inspect_images([
            make_image(self.root / "z.jpg", "JPEG"), make_image(self.root / "a.png"),
        ])
        self.output = self.root / "submissions"
        self.app = mock.create_app(self.images, output_dir=self.output, clock=lambda: self.now)
        self.client = TestClient(self.app, headers={"Authorization": "Bearer local-test-token"})
        self.addCleanup(self.client.close)

    def fetch(self, mode=None):
        response = self.client.get("/api/v1/image", params={"mode": mode} if mode else {})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def payload(self, image, objects=None):
        return {"img_id": image.headers["X-Image-ID"], "predictions": {
            "image_name": image.headers["X-Image-Name"],
            "objects": objects if objects is not None else [
                {"id": 1, "class": "good", "polygon": [[1, 1], [10, 1], [5, 8]]},
            ],
        }}

    def stats(self):
        response = self.client.get("/api/v1/stats")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def configure(self, **settings):
        response = self.client.patch("/mock/config", json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_image_binary_headers_sorted_rotation_and_fresh_ids(self):
        first, second, third = self.fetch(), self.fetch(), self.fetch()
        self.assertEqual([r.headers["X-Image-Name"] for r in (first, second, third)], ["a.png", "z.jpg", "a.png"])
        self.assertEqual(first.content, self.images[0].path.read_bytes())
        self.assertEqual(first.headers["content-type"], "image/png")
        self.assertEqual(second.headers["content-type"], "image/jpeg")
        self.assertEqual(first.headers["X-Mode"], "train")
        self.assertEqual(len({r.headers["X-Image-ID"] for r in (first, second, third)}), 3)

    def test_submission_response_saved_payload_latency_and_stats(self):
        image = self.fetch()
        self.now += 2.25
        payload = self.payload(image)
        response = self.client.post("/api/v1/submit", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["duration_seconds"], 2.25)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["image_name"], "a.png")
        self.assertEqual(data["mode"], "train")
        self.assertTrue(data["received_at"].endswith("+00:00"))
        saved = json.loads((self.output / f"{payload['img_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"], payload)
        self.assertEqual(saved["response"], data)
        self.assertEqual(self.stats(), {
            "team_name": "TEAM_33", "train_mode": {"images_requested": 1, "submissions_received": 1},
            "test_mode": {"quota_limit": 5, "images_requested": 0, "submissions_received": 0, "remaining_attempts": 5},
        })

    def test_empty_objects_and_all_seven_classes_are_accepted(self):
        classes = ["good", "shell", "black", "husk", "broken", "parchment", "immature"]
        objects = [{"id": i + 1, "class": cls, "polygon": [[1, 1], [2, 1], [1, 2]]}
                   for i, cls in enumerate(classes)]
        for value in (objects, []):
            response = self.client.post("/api/v1/submit", json=self.payload(self.fetch(), value))
            self.assertEqual(response.status_code, 200, response.text)

    def test_authentication_on_every_endpoint(self):
        routes = [("GET", "/api/v1/image", None), ("GET", "/api/v1/stats", None),
                  ("POST", "/api/v1/submit", {"img_id": "x", "predictions": {"image_name": "x", "objects": []}}),
                  ("PATCH", "/mock/config", {"paused": True}), ("POST", "/mock/reset", None)]
        with TestClient(self.app) as anonymous:
            for method, route, body in routes:
                for headers in ({}, {"Authorization": "Bearer wrong"}):
                    with self.subTest(method=method, route=route, headers=headers):
                        response = anonymous.request(method, route, json=body, headers=headers)
                        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.stats()["train_mode"]["images_requested"], 0)

    def test_default_mode_and_explicit_mode(self):
        self.configure(default_mode="test")
        self.assertEqual(self.fetch().headers["X-Mode"], "test")
        self.assertEqual(self.fetch("train").headers["X-Mode"], "train")
        self.assertEqual(self.client.get("/api/v1/image?mode=invalid").status_code, 422)

    def test_quota_counts_successful_gets_and_still_allows_last_submit(self):
        self.configure(quota_limit=1)
        image = self.fetch("test")
        self.assertEqual(self.client.get("/api/v1/image?mode=test").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/submit", json=self.payload(image)).status_code, 200)
        self.assertEqual(self.stats()["test_mode"], {
            "quota_limit": 1, "images_requested": 1, "submissions_received": 1, "remaining_attempts": 0,
        })
        self.fetch("train")

    def test_parallel_gets_do_not_exceed_test_quota(self):
        self.configure(quota_limit=2)
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(pool.map(lambda _: self.client.get("/api/v1/image?mode=test"), range(5)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 200, 403, 403, 403])
        self.assertEqual(self.stats()["test_mode"]["images_requested"], 2)

    def test_timeout_without_sleep_and_failed_submit_does_not_count(self):
        self.configure(timeout_seconds=1)
        image = self.fetch()
        self.now += 1.01
        self.assertEqual(self.client.post("/api/v1/submit", json=self.payload(image)).status_code, 408)
        self.assertEqual(self.stats()["train_mode"]["submissions_received"], 0)
        self.assertFalse(self.output.exists())

    def test_duplicate_submit_is_409_and_does_not_overwrite_result(self):
        payload = self.payload(self.fetch())
        self.assertEqual(self.client.post("/api/v1/submit", json=payload).status_code, 200)
        original = next(self.output.glob("*.json")).read_bytes()
        self.now += 100
        self.assertEqual(self.client.post("/api/v1/submit", json=payload).status_code, 409)
        self.assertEqual(next(self.output.glob("*.json")).read_bytes(), original)
        self.assertEqual(self.stats()["train_mode"]["submissions_received"], 1)

    def test_closed_modes_and_pause_resume_preserve_stats(self):
        image = self.fetch()
        self.configure(enabled_modes=["test"])
        self.assertEqual(self.client.get("/api/v1/image?mode=train").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/submit", json=self.payload(image)).status_code, 403)
        self.configure(paused=True)
        self.assertEqual(self.client.get("/api/v1/image?mode=test").status_code, 503)
        self.assertEqual(self.client.post("/api/v1/submit", json=self.payload(image)).status_code, 503)
        self.assertEqual(self.stats()["train_mode"]["images_requested"], 1)
        self.configure(paused=False, enabled_modes=["train", "test"])
        self.assertEqual(self.client.post("/api/v1/submit", json=self.payload(image)).status_code, 200)

    def test_unknown_id_and_wrong_filename(self):
        payload = self.payload(self.fetch())
        original_id = payload["img_id"]
        payload["img_id"] = "../../outside"
        self.assertEqual(self.client.post("/api/v1/submit", json=payload).status_code, 400)
        payload["img_id"] = original_id
        payload["predictions"]["image_name"] = "wrong.png"
        self.assertEqual(self.client.post("/api/v1/submit", json=payload).status_code, 400)

    def test_malformed_predictions_are_422(self):
        valid = self.payload(self.fetch())
        modifications = [
            lambda p: p["predictions"]["objects"][0].update({"class": "unknown"}),
            lambda p: p["predictions"]["objects"].append(p["predictions"]["objects"][0].copy()),
            lambda p: p["predictions"]["objects"][0].update(id=True),
            lambda p: p["predictions"]["objects"][0].update(polygon=[[1, 2], [3, 4]]),
            lambda p: p["predictions"]["objects"][0].update(polygon=[[1, 2, 3]] * 3),
            lambda p: p["predictions"]["objects"][0].update(polygon=[["1", 2]] * 3),
            lambda p: p["predictions"]["objects"][0].update(polygon=[[True, 2]] * 3),
            lambda p: p.update(predictions={"image_name": "a.png"}),
        ]
        for change in modifications:
            candidate = json.loads(json.dumps(valid))
            change(candidate)
            with self.subTest(payload=candidate):
                self.assertEqual(self.client.post("/api/v1/submit", json=candidate).status_code, 422)
        self.assertEqual(self.stats()["train_mode"]["submissions_received"], 0)

    def test_invalid_config_is_rejected_without_changing_settings(self):
        for payload in ({"quota_limit": -1}, {"timeout_seconds": 0}, {"paused": None},
                        {"default_mode": "other"}, {"enabled_modes": ["other"]}, {"typo": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch("/mock/config", json=payload).status_code, 422)
        self.assertEqual(self.configure()["default_mode"], "train")
        self.assertEqual(self.configure()["quota_limit"], 5)

    def test_reset_invalidates_rounds_preserves_files_settings_and_rotation(self):
        image = self.fetch()
        self.client.post("/api/v1/submit", json=self.payload(image))
        self.configure(quota_limit=2)
        self.assertEqual(self.client.post("/mock/reset").status_code, 200)
        self.assertEqual(self.stats()["train_mode"]["images_requested"], 0)
        self.assertEqual(self.stats()["test_mode"]["quota_limit"], 2)
        self.assertEqual(len(list(self.output.glob("*.json"))), 1)
        self.assertEqual(self.client.post("/api/v1/submit", json=self.payload(image)).status_code, 400)
        self.assertEqual(self.fetch().headers["X-Image-Name"], "a.png")

    def test_failed_image_delivery_releases_quota(self):
        self.images[0].path.unlink()
        with self.assertRaises(RuntimeError):
            self.client.get("/api/v1/image?mode=test")
        self.assertEqual(self.stats()["test_mode"]["images_requested"], 0)
        self.assertEqual(self.app.state.mock.rounds, {})

    def test_failed_save_does_not_accept_submission(self):
        payload = self.payload(self.fetch())
        with patch.object(mock, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.client.post("/api/v1/submit", json=payload)
        self.assertEqual(self.stats()["train_mode"]["submissions_received"], 0)
        self.assertEqual(self.client.post("/api/v1/submit", json=payload).status_code, 200)


class CacheTests(unittest.TestCase):
    URL = "https://drive.google.com/drive/folders/example-folder?usp=sharing"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-cache-", dir=ENDPOINT_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entries = [SimpleNamespace(id="one", path="nested/a.png"),
                        SimpleNamespace(id="two", path="b.jpg"),
                        SimpleNamespace(id="text", path="notes.txt")]

        def download(**kwargs):
            make_image(Path(kwargs["output"]), "JPEG" if kwargs["id"] == "two" else "PNG")
            return kwargs["output"]

        self.gdown = SimpleNamespace(download_folder=Mock(side_effect=lambda **kwargs: self.entries),
                                     download=Mock(side_effect=download))
        self.gdown_patch = patch.dict(sys.modules, {"gdown": self.gdown})
        self.gdown_patch.start()
        self.addCleanup(self.gdown_patch.stop)

    def prepare(self, refresh=False, url=None):
        return mock.prepare_images(url or self.URL, refresh=refresh, cache_root=self.root)

    def test_download_recursive_images_only_and_reuse_verified_cache_offline(self):
        images = self.prepare()
        self.assertEqual([image.path.name for image in images], ["a.png", "b.jpg"])
        self.assertEqual(self.gdown.download.call_count, 2)
        self.assertFalse(self.gdown.download_folder.call_args.kwargs["use_cookies"])
        self.assertFalse(self.gdown.download_folder.call_args.kwargs["remaining_ok"])
        self.gdown.download_folder.side_effect = AssertionError("cache must not access network")
        self.assertEqual(self.prepare(), images)

    def test_partial_download_is_not_published_and_failed_refresh_preserves_old_cache(self):
        previous = self.prepare()
        active = next(self.root.rglob("active.json"))
        manifest = active.read_bytes()
        self.gdown.download.side_effect = lambda **kwargs: None
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.prepare(refresh=True)
        self.assertEqual(active.read_bytes(), manifest)
        self.assertEqual(self.prepare(), previous)

    def test_first_download_failure_does_not_create_active_manifest(self):
        self.gdown.download.side_effect = lambda **kwargs: None
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.prepare()
        self.assertFalse(list(self.root.rglob("active.json")))

    def test_refresh_replaces_active_snapshot_and_keeps_previous_images(self):
        previous = self.prepare()
        refreshed = self.prepare(refresh=True)
        self.assertNotEqual(previous[0].path, refreshed[0].path)
        self.assertTrue(all(image.path.is_file() for image in previous + refreshed))
        self.assertEqual(self.prepare(), refreshed)

    def test_cache_missing_or_modified_image_requires_refresh(self):
        images = self.prepare()
        images[0].path.write_bytes(b"incomplete")
        with self.assertRaisesRegex(ValueError, "--refresh"):
            self.prepare()
        self.prepare(refresh=True)

    def test_empty_failed_and_truncated_listing_do_not_publish_cache(self):
        for value in (None, [], [SimpleNamespace(id="text", path="notes.txt")]):
            with self.subTest(entries=value):
                self.entries = value
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(list(self.root.rglob("active.json")))
        self.gdown.download_folder.side_effect = RuntimeError("Too many files; incomplete listing")
        with self.assertRaisesRegex(RuntimeError, "incomplete listing"):
            self.prepare()

    def test_duplicate_names_and_path_traversal_are_rejected_before_download(self):
        for paths in (["a.png", "nested/A.PNG"], ["../outside.png"]):
            with self.subTest(paths=paths):
                self.entries = [SimpleNamespace(id=str(i), path=path) for i, path in enumerate(paths)]
                with self.assertRaises(ValueError):
                    self.prepare()
        self.gdown.download.assert_not_called()

    def test_html_instead_of_image_is_not_cached(self):
        def bad_download(**kwargs):
            Path(kwargs["output"]).write_text("<html>login required</html>")
            return kwargs["output"]
        self.gdown.download.side_effect = bad_download
        with self.assertRaises(OSError):
            self.prepare()
        self.assertFalse(list(self.root.rglob("active.json")))

    def test_non_header_safe_filename_fails_with_actionable_message(self):
        with self.assertRaisesRegex(ValueError, "ASCII filename"):
            mock.inspect_images([make_image(self.root / "กาแฟ.png")])

    def test_different_drive_folders_have_separate_caches(self):
        first = self.prepare()
        second = self.prepare(url="https://drive.google.com/drive/folders/another-folder")
        self.assertNotEqual(first[0].path, second[0].path)
        self.assertEqual(len(list(self.root.rglob("active.json"))), 2)

    def test_invalid_drive_url_is_rejected(self):
        for url in ("https://example.com/drive/folders/a", "http://drive.google.com/drive/folders/a",
                    "https://drive.google.com/file/d/a/view"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.prepare(url=url)
        self.gdown.download_folder.assert_not_called()

    def test_default_directories_are_relative_to_script(self):
        self.assertEqual(mock.prepare_images.__kwdefaults__["cache_root"], ENDPOINT_DIR / "cache")
        self.assertEqual(mock.create_app.__kwdefaults__["output_dir"], ENDPOINT_DIR / "submissions")


class ClientSmokeTests(unittest.TestCase):
    def test_original_client_over_real_local_http_without_mqtt(self):
        import uvicorn

        with tempfile.TemporaryDirectory(prefix=".test-client-", dir=ENDPOINT_DIR) as directory:
            root = Path(directory)
            images = mock.inspect_images([make_image(root / "input" / "smoke.png")])
            app = mock.create_app(images, output_dir=root / "submissions")
            original_client = ENDPOINT_DIR.parent / "client.py"
            original_hash = hashlib.sha256(original_client.read_bytes()).hexdigest()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
                thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not server.started and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(server.started, "Local mock server failed to start")
                    result = subprocess.run([
                        sys.executable, str(original_client), "--http-url", f"http://127.0.0.1:{port}",
                        "--http-token", "local-test-token", "--skip-mqtt",
                    ], cwd=root, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("HTTP Submission ACCEPTED", result.stdout)
                    self.assertIn("MQTT sending skipped", result.stdout)
                    self.assertIn("Requested: 1 | Submitted: 1", result.stdout)
                    self.assertTrue((root / "downloaded_images" / "smoke.png").is_file())
                    self.assertEqual(len(list((root / "submissions").glob("*.json"))), 1)
                    self.assertEqual(hashlib.sha256(original_client.read_bytes()).hexdigest(), original_hash)
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive(), "Smoke test server did not stop")


if __name__ == "__main__":
    unittest.main()
