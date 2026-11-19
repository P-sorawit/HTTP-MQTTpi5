"""Local TopGun HTTP simulator. All generated data stays beside this script."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


BASE_DIR = Path(__file__).resolve().parent
Mode = Literal["train", "test"]
BeanClass = Literal["good", "shell", "black", "husk", "broken", "parchment", "immature"]
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


class PayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BeanObject(PayloadModel):
    id: Annotated[StrictInt, Field(ge=1)]
    class_name: BeanClass = Field(alias="class")
    polygon: Annotated[list[tuple[Coordinate, Coordinate]], Field(min_length=3)]


class Predictions(PayloadModel):
    image_name: Annotated[StrictStr, Field(min_length=1)]
    objects: list[BeanObject]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [obj.id for obj in self.objects]
        if len(ids) != len(set(ids)):
            raise ValueError("Object IDs must be unique within an image")
        return self


class Submission(PayloadModel):
    img_id: Annotated[StrictStr, Field(min_length=1)]
    predictions: Predictions


class Settings(PayloadModel):
    default_mode: Mode = "train"
    enabled_modes: list[Mode] = Field(default_factory=lambda: ["train", "test"])
    paused: bool = False
    quota_limit: Annotated[StrictInt, Field(ge=0)] = 5
    timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 60.0


class SettingsPatch(PayloadModel):
    default_mode: Mode | None = None
    enabled_modes: list[Mode] | None = None
    paused: bool | None = None
    quota_limit: Annotated[StrictInt, Field(ge=0)] | None = None
    timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def no_explicit_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Omit unchanged settings; null is not supported")
        return self


@dataclass(frozen=True)
class TestImage:
    path: Path
    media_type: str


def safe_child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError(f"Path escapes the image directory: {relative!r}")
    return path


def inspect_images(paths: list[Path]) -> list[TestImage]:
    """Validate all supported images, including duplicate/header-safe names."""
    images = []
    names = set()
    for path in paths:
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        name = path.name
        if name.casefold() in names:
            raise ValueError(f"Duplicate image filename (case insensitive): {name}")
        names.add(name.casefold())
        # requests interprets custom HTTP headers as Latin-1. Never silently
        # rename an image or return a name that the unchanged client misreads.
        try:
            name.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError(f"Rename {name!r} to an ASCII filename for X-Image-Name") from exc
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError(f"Image filename contains an HTTP control character: {name!r}")
        with Image.open(path) as image:
            image_format = image.format
            if image_format not in {"JPEG", "PNG"}:
                raise ValueError(f"Not a JPEG/PNG image: {path}")
            image.verify()
        # verify() checks structure; load() also detects truncated pixel data.
        with Image.open(path) as image:
            image.load()
        images.append(TestImage(path.resolve(), "image/jpeg" if image_format == "JPEG" else "image/png"))
    if not images:
        raise ValueError("No readable JPEG/PNG images found")
    return sorted(images, key=lambda item: (item.path.name.casefold(), item.path.name))


def write_json(path: Path, data: dict):
    """Publish complete files only, including the active-cache pointer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def folder_id_from_url(url: str) -> str:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "drive.google.com" or not match:
        raise ValueError("Use an https://drive.google.com/drive/folders/<ID> folder link")
    return match.group(1)


def prepare_images(
    drive_folder: str,
    *,
    refresh: bool = False,
    cache_root: Path = BASE_DIR / "cache",
) -> list[TestImage]:
    """Cache a complete public Drive image listing; never reuse partial downloads."""
    folder_id = folder_id_from_url(drive_folder)
    source_dir = cache_root / hashlib.sha256(folder_id.encode()).hexdigest()[:20]
    source_dir.mkdir(parents=True, exist_ok=True)
    from filelock import FileLock

    with FileLock(str(source_dir / ".download.lock"), timeout=10):
        active = source_dir / "active.json"
        if active.exists() and not refresh:
            try:
                manifest = json.loads(active.read_text(encoding="utf-8"))
                if manifest["version"] != 1 or manifest["folder_id"] != folder_id:
                    raise ValueError("Cache manifest does not match the Drive folder")
                snapshot = safe_child(source_dir, manifest["snapshot"])
                paths = []
                for entry in manifest["images"]:
                    path = safe_child(snapshot, entry["path"])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                        raise ValueError(f"Cached image changed: {path.name}")
                    paths.append(path)
                return inspect_images(paths)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise ValueError(f"Cache is incomplete or damaged; rerun with --refresh: {exc}") from exc

        import gdown

        with tempfile.TemporaryDirectory(prefix=".download-", dir=source_dir) as staging:
            dataset = Path(staging) / "images"
            dataset.mkdir()
            # remaining_ok=False makes gdown refuse a truncated folder listing
            # (including its 50-items-per-folder limit) instead of claiming success.
            entries = gdown.download_folder(
                url=drive_folder, output=str(dataset), skip_download=True,
                use_cookies=False, remaining_ok=False,
            )
            if entries is None:
                raise ValueError("Could not list the complete public Drive folder")
            selected = []
            for entry in entries:
                path = safe_child(dataset, entry.path)
                if path.suffix.lower() in IMAGE_SUFFIXES:
                    selected.append((entry, path))
            if not selected:
                raise ValueError("The Drive folder contains no JPEG/PNG images")
            names = [path.name.casefold() for _, path in selected]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate image filenames in the Drive folder/subfolders")
            for entry, path in selected:
                path.parent.mkdir(parents=True, exist_ok=True)
                result = gdown.download(id=entry.id, output=str(path), use_cookies=False)
                if result is None or not path.is_file():
                    raise ValueError(f"Incomplete Drive download: {entry.path}")
            images = inspect_images([path for _, path in selected])
            manifest = {
                "version": 1,
                "folder_id": folder_id,
                "snapshot": f"snapshot-{uuid.uuid4().hex}",
                "images": [
                    {"path": item.path.relative_to(dataset).as_posix(),
                     "sha256": hashlib.sha256(item.path.read_bytes()).hexdigest()}
                    for item in images
                ],
            }
            # Retain previous snapshots. A failed refresh cannot destroy a good cache.
            snapshot = source_dir / manifest["snapshot"]
            dataset.replace(snapshot)
            write_json(active, manifest)
            return [TestImage(snapshot / item.path.relative_to(dataset), item.media_type) for item in images]


@dataclass
class ImageRound:
    image: TestImage
    mode: Mode
    delivered_at: float | None = None
    submitted: bool = False


class MockState:
    def __init__(self, images, team_id, output_dir, clock):
        self.images = images
        self.team_id = team_id
        self.output_dir = output_dir
        self.clock = clock
        self.settings = Settings()
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.rounds: dict[str, ImageRound] = {}
        self.cursor = 0
        self.requested = {"train": 0, "test": 0}
        self.submitted = {"train": 0, "test": 0}

    def check_available(self, mode: Mode):
        if self.settings.paused:
            raise HTTPException(503, "Mock server is paused")
        if mode not in self.settings.enabled_modes:
            raise HTTPException(403, f"Mode {mode} is closed")


class TimedImageResponse(FileResponse):
    def __init__(self, *, state: MockState, img_id: str, record: ImageRound):
        super().__init__(path=record.image.path, media_type=record.image.media_type, headers={
            "X-Image-ID": img_id, "X-Image-Name": record.image.path.name,
            "X-Mode": record.mode, "Cache-Control": "no-store",
        })
        self.state, self.img_id, self.record = state, img_id, record

    async def __call__(self, scope, receive, send):
        async def tracked_send(message):
            await send(message)
            if ((message["type"] == "http.response.body" and not message.get("more_body", False))
                    or message["type"] == "http.response.pathsend"):
                with self.state.lock:
                    self.record.delivered_at = self.state.clock()
        try:
            await super().__call__(scope, receive, tracked_send)
        except BaseException:
            with self.state.lock:
                if self.record.delivered_at is None and self.state.rounds.get(self.img_id) is self.record:
                    self.state.rounds.pop(self.img_id)
                    self.state.requested[self.record.mode] -= 1
            raise


def create_app(
    images: list[TestImage], *, token: str = "local-test-token", team_id: str = "TEAM_33",
    output_dir: Path = BASE_DIR / "submissions", clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    if not token.strip():
        raise ValueError("Mock token must not be empty")
    if not images:
        raise ValueError("At least one validated image is required")
    state = MockState(images, team_id, output_dir, clock)

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        supplied = (authorization or "").encode("utf-8")
        if not secrets.compare_digest(supplied, f"Bearer {token}".encode("utf-8")):
            raise HTTPException(401, "Invalid mock Bearer token", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="TopGun local mock", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(authenticate)])
    app.state.mock = state

    @app.get("/api/v1/image")
    def get_image(mode: Mode | None = None):
        with state.lock:
            selected_mode = mode or state.settings.default_mode
            state.check_available(selected_mode)
            if selected_mode == "test" and state.requested["test"] >= state.settings.quota_limit:
                raise HTTPException(403, "Test image quota exhausted")
            record = ImageRound(state.images[state.cursor % len(state.images)], selected_mode)
            img_id = f"img_{uuid.uuid4().hex}"
            response = TimedImageResponse(state=state, img_id=img_id, record=record)
            state.cursor += 1
            state.rounds[img_id] = record
            # Reserve quota before streaming, release if delivery fails.
            state.requested[selected_mode] += 1
            return response

    @app.post("/api/v1/submit")
    def submit(payload: Submission):
        with state.lock:
            if state.settings.paused:
                raise HTTPException(503, "Mock server is paused")
            record = state.rounds.get(payload.img_id)
            if record is None:
                raise HTTPException(400, "Unknown img_id (server restart/reset invalidates IDs)")
            state.check_available(record.mode)
            if record.submitted:
                raise HTTPException(409, "Submission already recorded for this img_id")
            if record.delivered_at is None:
                raise HTTPException(400, "Image delivery is not complete")
            if payload.predictions.image_name != record.image.path.name:
                raise HTTPException(400, "image_name must match X-Image-Name")
            duration = state.clock() - record.delivered_at
            if duration > state.settings.timeout_seconds:
                raise HTTPException(408, "Submission exceeded the mock time limit")
            response = {
                "status": "success", "message": "Submission recorded successfully",
                "img_id": payload.img_id, "image_name": record.image.path.name, "mode": record.mode,
                "duration_seconds": round(duration, 6),
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            write_json(state.output_dir / f"{payload.img_id}.json", {
                "request": payload.model_dump(by_alias=True), "response": response,
            })
            record.submitted = True
            state.submitted[record.mode] += 1
            return response

    @app.get("/api/v1/stats")
    def stats():
        with state.lock:
            return {
                "team_name": state.team_id,
                "train_mode": {"images_requested": state.requested["train"],
                               "submissions_received": state.submitted["train"]},
                "test_mode": {"quota_limit": state.settings.quota_limit,
                              "images_requested": state.requested["test"],
                              "submissions_received": state.submitted["test"],
                              "remaining_attempts": max(0, state.settings.quota_limit - state.requested["test"])},
            }

    @app.patch("/mock/config")
    def configure(patch: SettingsPatch):
        with state.lock:
            state.settings = Settings.model_validate({
                **state.settings.model_dump(), **patch.model_dump(exclude_unset=True),
            })
            return state.settings.model_dump()

    @app.post("/mock/reset")
    def reset():
        with state.lock:
            state.reset()
            return {"status": "success", "message": "Rounds and statistics reset; settings and saved files retained"}

    return app


def main():
    parser = argparse.ArgumentParser(description="Local TopGun HTTP mock using a public Google Drive folder")
    parser.add_argument("--drive-folder", required=True, help="Public Google Drive folder URL")
    parser.add_argument("--token", default="local-test-token", help="Mock Bearer token, separate from real credentials")
    parser.add_argument("--team-id", default="TEAM_33")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--refresh", action="store_true", help="Download a new snapshot instead of using the cache")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        if not args.token.strip():
            raise ValueError("Mock token must not be empty")
        images = prepare_images(args.drive_folder, refresh=args.refresh)
        app = create_app(images, token=args.token, team_id=args.team_id)
    except Exception as exc:
        parser.exit(1, f"Mock startup failed: {exc}\nCheck Drive sharing, network access, image names, or use --refresh.\n")
    print(f"Loaded {len(images)} images. Mock API: http://127.0.0.1:{args.port}")
    print(f"Saved submissions: {BASE_DIR / 'submissions'}")
    print("Local simulation only; model predictions are not scored. Stop with Ctrl+C.")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
