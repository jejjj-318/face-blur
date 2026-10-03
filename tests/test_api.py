import json
import hashlib
import subprocess
import time

import cv2
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.video import probe


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as value:
        yield value


@pytest.fixture
def movie(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def wait_job(client, identifier, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = client.get(f"/api/videos/{identifier}").json()
        if value["status"] not in {
            "uploading",
            "queued",
            "normalizing",
            "analyzing",
            "rendering",
        }:
            return value
        time.sleep(0.05)
    pytest.fail("Обработка не завершилась вовремя")


def upload(client, path):
    with path.open("rb") as file:
        response = client.post(
            "/api/videos", files={"file": (path.name, file, "video/mp4")}
        )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_complete_pipeline_preview_audio_range_download_and_repeat(
    client, movie, tmp_path
):
    identifier = upload(client, movie)
    job = wait_job(client, identifier)
    assert job["status"] == "ready", job
    assert job["info"]["frames"] == 10
    assert job["info"]["audio"]
    settings = {
        "strength": 90,
        "manual": [
            {
                "id": "zone",
                "start": 0,
                "end": 1,
                "keyframes": [
                    {"time": 0, "box": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}
                ],
            }
        ],
    }
    unblurred = client.post(
        f"/api/videos/{identifier}/preview",
        json={**settings, "time": 0.2, "blurred": False},
    )
    blurred = client.post(
        f"/api/videos/{identifier}/preview", json={**settings, "time": 0.2}
    )
    assert (
        blurred.status_code == 200 and blurred.headers["content-type"] == "image/jpeg"
    )
    assert blurred.content != unblurred.content
    assert (
        client.post(f"/api/videos/{identifier}/render", json=settings).status_code
        == 202
    )
    job = wait_job(client, identifier)
    assert job["status"] == "complete", job
    response = client.get(f"/api/videos/{identifier}/result?download=true")
    assert "source_blurred.mp4" in response.headers["content-disposition"]
    result = tmp_path / "result.mp4"
    result.write_bytes(response.content)
    info = probe(result)
    assert info["audio"] and info["duration"] == pytest.approx(1, abs=0.1)
    normalized = tmp_path / "normalized.mp4"
    normalized.write_bytes(client.get(f"/api/videos/{identifier}/source").content)

    def audio_hash(path):
        content = subprocess.check_output(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                "-f",
                "adts",
                "pipe:1",
            ]
        )
        return hashlib.sha256(content).digest()

    assert audio_hash(normalized) == audio_hash(result)
    cap = cv2.VideoCapture(str(result))
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == 10
    cap.release()
    ranged = client.get(
        f"/api/videos/{identifier}/result", headers={"Range": "bytes=0-99"}
    )
    assert ranged.status_code == 206 and len(ranged.content) == 100
    assert (
        client.post(
            f"/api/videos/{identifier}/render", json={"strength": 40}
        ).status_code
        == 202
    )
    assert wait_job(client, identifier)["status"] == "complete"
    assert client.delete(f"/api/videos/{identifier}").status_code == 200
    assert client.get(f"/api/videos/{identifier}").status_code == 404


def test_invalid_uploads_and_corrupt_video(client):
    assert (
        client.post("/api/videos", files={"file": ("a.txt", b"bad")}).status_code == 400
    )
    assert (
        client.post("/api/videos", files={"file": ("empty.mp4", b"")}).status_code
        == 400
    )
    response = client.post(
        "/api/videos", files={"file": ("broken.mp4", b"not a video")}
    )
    assert response.status_code == 202
    assert wait_job(client, response.json()["id"])["status"] == "failed"


def test_unknown_job_and_foreign_origin(client):
    assert client.get("/api/videos/..").status_code in {404, 307}
    assert client.get("/api/videos/" + "0" * 32).status_code == 404
    assert (
        client.post(
            "/api/videos",
            headers={"Origin": "https://other.example"},
            files={"file": ("a.mp4", b"a")},
        ).status_code
        == 403
    )


def test_invalid_parameters(client, movie):
    identifier = upload(client, movie)
    wait_job(client, identifier)
    endpoint = f"/api/videos/{identifier}/render"
    for value in (
        {"strength": 0},
        {"strength": 101},
        {"strength": 70, "unexpected": True},
    ):
        assert client.post(endpoint, json=value).status_code == 422
    assert client.post(endpoint, json={"auto": [{"id": 999}]}).status_code == 409
    assert (
        client.post(f"/api/videos/{identifier}/preview", json={"time": 2}).status_code
        == 400
    )


def test_body_limit_also_checks_streamed_json(client):
    # Нет доверия к Content-Length; лимит срабатывает при чтении тела.
    response = client.post(
        "/api/videos/" + "0" * 32 + "/render",
        content=(b" " * 600000 for _ in range(2)),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413, response.text


def test_oversized_upload_limit_before_multipart_parse(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "MAX_UPLOAD", 100)
    response = client.post(
        "/api/videos", files={"file": ("large.mp4", b"a" * (1024**2 + 1000))}
    )
    assert response.status_code == 413, response.text


def test_busy_job_cannot_be_deleted_or_rendered_again(client):
    manager = client.app.state.jobs
    identifier = manager.create("busy.mp4")
    assert client.delete(f"/api/videos/{identifier}").status_code == 409
    assert client.post(f"/api/videos/{identifier}/render", json={}).status_code == 409


def test_rotation_metadata_is_applied(client, movie, tmp_path):
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-display_rotation",
            "90",
            "-i",
            str(movie),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
    )
    job = wait_job(client, upload(client, rotated))
    assert job["status"] == "ready", job
    assert (job["info"]["width"], job["info"]["height"]) == (240, 320)


def test_non_square_pixels_preserve_display_aspect_ratio(client, tmp_path):
    path = tmp_path / "wide.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=size=320x240:rate=10:duration=0.5",
            "-vf",
            "setsar=2",
            "-c:v",
            "libx264",
            str(path),
        ],
        check=True,
    )
    job = wait_job(client, upload(client, path))
    assert job["status"] == "ready", job
    assert (job["info"]["width"], job["info"]["height"]) == (640, 240)


def test_restart_marks_interrupted_jobs(tmp_path):
    root = tmp_path / "data"
    directory = root / ("a" * 32)
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(
        json.dumps(
            {"id": "a" * 32, "filename": "a.mp4", "status": "analyzing", "progress": 30}
        )
    )
    with TestClient(create_app(root)) as client:
        job = client.get("/api/videos/" + "a" * 32).json()
        assert job["status"] == "failed" and "прервалась" in job["error"]


def test_restart_recovers_interrupted_export(tmp_path):
    root = tmp_path / "data"
    directory = root / ("b" * 32)
    directory.mkdir(parents=True)
    (directory / "tracks.json").write_text("[]")
    (directory / "job.json").write_text(
        json.dumps(
            {
                "id": "b" * 32,
                "filename": "b.mp4",
                "status": "rendering",
                "progress": 30,
                "settings": {"strength": 70},
            }
        )
    )
    with TestClient(create_app(root)) as client:
        job = client.get("/api/videos/" + "b" * 32).json()
        assert job["status"] == "ready" and job["settings"]["strength"] == 70
