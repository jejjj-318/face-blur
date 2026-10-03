"""Проверка, нормализация, обнаружение лиц и кодирование видео."""

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import cv2

from .masks import MaskPlan, Tracker, blur_frame, iou

MODEL = Path(__file__).resolve().parent.parent / "models" / "yunet.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
cv2.setNumThreads(2)


def probe(path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        info = json.loads(result.stdout)
        stream = next(
            s
            for s in info["streams"]
            if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")
        )
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(stream.get("duration", info["format"].get("duration", 0)))
        fps = Fraction(stream.get("avg_frame_rate", "0/1"))
        if fps <= 0:
            fps = Fraction(stream["r_frame_rate"])
        if not (
            0 < duration <= 600
            and 1 <= fps <= 60
            and 16 <= width <= 4096
            and 16 <= height <= 4096
            and width * height <= 4096 * 2160
        ):
            raise ValueError(
                "Поддерживаются видео до 10 минут, 60 кадров/с и разрешения 4K"
            )
        return {
            "width": width,
            "height": height,
            "duration": duration,
            "fps": str(fps),
            "stream_index": stream["index"],
            "audio": any(s["codec_type"] == "audio" for s in info["streams"]),
        }
    except (
        subprocess.SubprocessError,
        StopIteration,
        KeyError,
        ZeroDivisionError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(
            "Не удалось прочитать видео. Проверьте формат и целостность файла"
        ) from exc


def normalize(source, destination, info, progress):
    # FFmpeg применяет поворот из метаданных. SAR учитывается до масштабирования.
    scale = "min(1,min(1920/(iw*sar),1920/ih))"
    filters = (
        f"scale=w='max(2,trunc({scale}*iw*sar/2)*2)':"
        f"h='max(2,trunc({scale}*ih/2)*2)',setsar=1,fps={info['fps']}"
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-map",
        f"0:{info['stream_index']}",
        "-map",
        "0:a:0?",
        "-vf",
        filters,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-af",
        "aresample=async=1:first_pts=0",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        str(destination),
    ]
    log_path = destination.with_suffix(".log")
    with log_path.open("wb") as log:
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=log, text=True
        ) as proc:
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.strip().split("=", 1)[1]) / 1_000_000
                        progress(min(0.99, seconds / info["duration"]))
                    except ValueError:
                        pass
            if proc.wait() != 0:
                raise ValueError("Не удалось преобразовать видео в формат для браузера")
    log_path.unlink(missing_ok=True)


class Detector:
    def __init__(self):
        if hashlib.sha256(MODEL.read_bytes()).hexdigest() != MODEL_SHA256:
            raise RuntimeError("Контрольная сумма модели YuNet не совпадает")
        self.model = cv2.FaceDetectorYN.create(
            str(MODEL), "", (320, 320), 0.65, 0.3, 5000
        )

    def detect(self, frame):
        height, width = frame.shape[:2]
        candidates = []
        # Два масштаба: первый сохраняет мелкие лица, второй помогает с крупными.
        for longest in (1280, 640):
            scale = min(1, longest / max(width, height))
            size = (max(32, round(width * scale)), max(32, round(height * scale)))
            image = (
                cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                if size != (width, height)
                else frame
            )
            self.model.setInputSize(size)
            _, faces = self.model.detect(image)
            if faces is not None:
                for face in faces:
                    x, y, w, h = face[:4]
                    candidates.append(
                        (
                            float(face[-1]),
                            [
                                float(x / size[0]),
                                float(y / size[1]),
                                float(w / size[0]),
                                float(h / size[1]),
                            ],
                        )
                    )
            if scale == 1 and max(width, height) <= 640:
                break
        kept = []
        for score, box in sorted(candidates, key=lambda item: item[0], reverse=True):
            if any(iou(box, other) >= 0.3 for other in kept):
                continue
            kept.append(box)
        padded = []
        for x, y, w, h in kept:
            x0, y0 = max(0, x - w * 0.2), max(0, y - h * 0.2)
            x1, y1 = min(1, x + w * 1.2), min(1, y + h * 1.2)
            if x1 > x0 and y1 > y0:
                padded.append([x0, y0, x1 - x0, y1 - y0])
        return padded


def analyze(path, info, progress):
    capture = cv2.VideoCapture(str(path))
    detector = Detector()
    fps = float(Fraction(info["fps"]))
    tracker = Tracker(hold_frames=max(1, round(fps * 0.2)))
    expected = max(1, round(info["duration"] * fps))
    frame_index, with_faces, detections = 0, 0, 0
    try:
        while True:
            success, frame = capture.read()
            if not success:
                break
            boxes = detector.detect(frame)
            with_faces += bool(boxes)
            detections += len(boxes)
            tracker.update(frame_index, boxes)
            frame_index += 1
            if frame_index % 5 == 0:
                progress(min(0.99, frame_index / expected))
            if len(tracker.tracks) > 1000:
                raise ValueError(
                    "Слишком много отдельных зон. Выберите более короткое видео"
                )
    finally:
        capture.release()
    if frame_index == 0 or frame_index < expected - 2:
        raise ValueError("Видео обрывается или не содержит читаемых кадров")
    return tracker.result(), {
        "frames": frame_index,
        "frames_with_faces": with_faces,
        "detections": detections,
        "duration": frame_index / fps,
    }


def preview(path, tracks, settings, info, time, blurred=True):
    fps = float(Fraction(info["fps"]))
    index = min(info["frames"] - 1, max(0, round(time * fps)))
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        success, frame = capture.read()
        if not success:
            raise ValueError("Не удалось прочитать выбранный кадр")
        if blurred:
            blur_frame(
                frame, MaskPlan(tracks, settings, fps).boxes(index), settings.strength
            )
        success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not success:
            raise ValueError("Не удалось подготовить предпросмотр")
        return encoded.tobytes()
    finally:
        capture.release()


def render(source, destination, tracks, settings, info, progress):
    fps = float(Fraction(info["fps"]))
    plan = MaskPlan(tracks, settings, fps)
    log_path = destination.with_suffix(".log")
    temporary = destination.with_name("rendering.mp4")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{info['width']}x{info['height']}",
        "-r",
        info["fps"],
        "-i",
        "pipe:0",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    capture = cv2.VideoCapture(str(source))
    count = 0
    try:
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
            )
            try:
                while True:
                    success, frame = capture.read()
                    if not success:
                        break
                    blur_frame(frame, plan.boxes(count), settings.strength)
                    proc.stdin.write(frame.tobytes())
                    count += 1
                    if count % 5 == 0:
                        progress(min(0.98, count / info["frames"]))
                proc.stdin.close()
                if proc.wait(timeout=120) != 0 or count != info["frames"]:
                    raise ValueError("Не удалось сохранить все кадры результата")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        result_info = probe(temporary)
        if abs(result_info["duration"] - info["duration"]) > max(0.1, 2 / fps):
            raise ValueError("Длительность результата отличается от исходного видео")
        temporary.replace(destination)
        log_path.unlink(missing_ok=True)
    finally:
        capture.release()
        temporary.unlink(missing_ok=True)
