"""Последовательная фоновая обработка и небольшое файловое хранилище."""

import copy
import json
import logging
import re
import shutil
import threading
import time
import uuid
from fractions import Fraction
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import video

BUSY = {"uploading", "queued", "normalizing", "analyzing", "rendering"}
MAX_UPLOAD = 250 * 1024**2
MAX_STORAGE = 4 * 1024**3
logger = logging.getLogger(__name__)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)


class Jobs:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video")
        self.items = {}
        for path in self.root.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["id"] != path.parent.name or not re.fullmatch(
                    r"[0-9a-f]{32}", job["id"]
                ):
                    continue
                job.setdefault("created_at", path.parent.stat().st_ctime)
                if job["status"] in BUSY:
                    if (
                        job["status"] == "rendering"
                        and (path.parent / "tracks.json").is_file()
                    ):
                        job.update(
                            status="ready",
                            progress=100,
                            error="Сохранение прервалось. Настройки сохранены — сформируйте видео заново.",
                        )
                    else:
                        job.update(
                            status="failed",
                            error="Обработка прервалась при остановке приложения. Загрузите видео заново.",
                        )
                    write_json(path, job)
                self.items[job["id"]] = job
            except (ValueError, KeyError, OSError):
                logger.warning("Пропущена повреждённая запись %s", path.name)

    def close(self):
        self.worker.shutdown(wait=False, cancel_futures=True)

    def get(self, identifier):
        with self.lock:
            if (
                not re.fullmatch(r"[0-9a-f]{32}", identifier)
                or identifier not in self.items
            ):
                raise KeyError(identifier)
            return copy.deepcopy(self.items[identifier])

    def directory(self, identifier):
        self.get(identifier)
        return self.root / identifier

    def update(self, identifier, persist=True, **changes):
        with self.lock:
            self.items[identifier].update(changes)
            if persist:
                write_json(self.root / identifier / "job.json", self.items[identifier])

    def capacity(self):
        used = sum(
            path.stat().st_size for path in self.root.glob("*/*") if path.is_file()
        )
        if (
            used + 2 * MAX_UPLOAD > MAX_STORAGE
            or shutil.disk_usage(self.root).free < 2 * MAX_UPLOAD
        ):
            raise ValueError("Недостаточно места. Удалите ненужные видео из списка")

    def create(self, filename):
        with self.lock:
            self.capacity()
            if len(self.items) >= 10:
                raise ValueError("В списке уже 10 видео. Удалите ненужные")
            if sum(j["status"] in BUSY for j in self.items.values()) >= 3:
                raise ValueError("В очереди уже 3 видео. Дождитесь окончания обработки")
            identifier = uuid.uuid4().hex
            (self.root / identifier).mkdir()
            name = Path(filename.replace("\\", "/")).name
            name = "".join(c for c in name if c.isprintable())[:160] or "video"
            self.items[identifier] = {
                "id": identifier,
                "filename": name,
                "status": "uploading",
                "progress": 0,
                "error": None,
                "created_at": time.time(),
            }
            self.update(identifier)
            return identifier

    def tracks(self, identifier):
        return json.loads(
            (self.directory(identifier) / "tracks.json").read_text(encoding="utf-8")
        )

    def submit_analysis(self, identifier):
        self.update(identifier, status="queued")
        self.worker.submit(self._analyze, identifier)

    def _analyze(self, identifier):
        directory = self.directory(identifier)
        try:
            original = directory / "upload.bin"
            info = video.probe(original)
            self.update(identifier, status="normalizing", info=info, progress=0)
            video.normalize(
                original,
                directory / "source.mp4",
                info,
                lambda p: self.update(
                    identifier, persist=False, progress=round(p * 25)
                ),
            )
            normalized = video.probe(directory / "source.mp4")
            tolerance = max(0.1, 2 / float(Fraction(info["fps"])))
            if abs(normalized["duration"] - info["duration"]) > tolerance:
                raise ValueError(
                    "Не удалось прочитать видео целиком. Проверьте целостность файла"
                )
            original.unlink()
            info = normalized
            self.update(identifier, status="analyzing", info=info, progress=25)
            tracks, stats = video.analyze(
                directory / "source.mp4",
                info,
                lambda p: self.update(
                    identifier, persist=False, progress=round(25 + p * 75)
                ),
            )
            info.update(stats)
            write_json(directory / "tracks.json", tracks)
            self.update(
                identifier,
                status="ready",
                progress=100,
                info=info,
                track_count=len(tracks),
            )
        except Exception as exc:
            logger.exception("Ошибка анализа %s", identifier)
            self.update(
                identifier,
                status="failed",
                error=(
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Ошибка обработки видео. Загрузите другой файл"
                ),
            )
            (directory / "upload.bin").unlink(missing_ok=True)

    def submit_render(self, identifier, settings):
        with self.lock:
            job = self.get(identifier)
            if job["status"] not in {"ready", "complete"}:
                raise ValueError("Видео ещё не готово к обработке")
            self.capacity()
            settings.check_video(self.tracks(identifier), job["info"]["duration"])
            self.update(
                identifier,
                status="rendering",
                progress=0,
                settings=settings.model_dump(),
                error=None,
            )
            self.worker.submit(self._render, identifier, settings.model_copy(deep=True))

    def _render(self, identifier, settings):
        directory = self.directory(identifier)
        try:
            job = self.get(identifier)
            video.render(
                directory / "source.mp4",
                directory / "result.mp4",
                self.tracks(identifier),
                settings,
                job["info"],
                lambda p: self.update(
                    identifier, persist=False, progress=round(p * 100)
                ),
            )
            self.update(identifier, status="complete", progress=100)
        except Exception as exc:
            logger.exception("Ошибка экспорта %s", identifier)
            self.update(
                identifier,
                status="ready",
                progress=100,
                error=(
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Не удалось сохранить результат. Попробуйте повторить обработку"
                ),
            )

    def delete(self, identifier):
        with self.lock:
            job = self.get(identifier)
            if job["status"] in BUSY:
                raise ValueError("Дождитесь завершения обработки перед удалением")
            directory = (self.root / identifier).resolve()
            if directory.parent != self.root.resolve():
                raise ValueError("Неверный каталог видео")
            shutil.rmtree(directory)
            del self.items[identifier]
