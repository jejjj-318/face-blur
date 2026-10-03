import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import video
from .jobs import Jobs, MAX_UPLOAD
from .masks import PreviewRequest, Settings

STATIC = Path(__file__).parent / "static"


class BodyLimit:
    """Ограничивает также запросы без Content-Length до разбора multipart."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD + 1024**2 if scope["path"] == "/api/videos" else 1024**2
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(
                    413, "Файл или запрос слишком большой. Видео: до 250 МБ"
                )
            return message

        await self.app(scope, limited_receive, send)


def create_app(data_dir=None):
    @asynccontextmanager
    async def lifespan(application):
        application.state.jobs = Jobs(data_dir or os.getenv("DATA_DIR", "data"))
        yield
        application.state.jobs.close()

    application = FastAPI(title="Face Blur", lifespan=lifespan)
    application.add_middleware(BodyLimit)

    @application.middleware("http")
    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            parsed = urlsplit(origin)
            if (
                parsed.netloc != request.headers.get("host")
                or parsed.scheme != request.url.scheme
            ):
                return JSONResponse(
                    {"detail": "Откройте приложение напрямую в браузере"}, 403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api/") else "no-cache"
        )
        return response

    def jobs(request):
        return request.app.state.jobs

    def get_job(request, identifier):
        try:
            return jobs(request).get(identifier)
        except KeyError:
            raise HTTPException(404, "Видео не найдено")

    def editable(request, identifier):
        job = get_job(request, identifier)
        if job["status"] not in {"ready", "complete"}:
            raise HTTPException(409, "Дождитесь окончания обработки видео")
        return job

    @application.get("/api/health")
    def health():
        return {"status": "ok"}

    @application.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @application.get("/api/videos")
    def list_videos(request: Request):
        manager = jobs(request)
        with manager.lock:
            return sorted(
                [manager.get(identifier) for identifier in manager.items],
                key=lambda value: value["created_at"],
                reverse=True,
            )

    @application.post("/api/videos", status_code=202)
    async def upload(request: Request, file: UploadFile = File(...)):
        manager = jobs(request)
        if Path((file.filename or "").lower()).suffix not in {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
        }:
            raise HTTPException(400, "Выберите видео MP4, MOV, MKV или WebM")
        try:
            identifier = manager.create(file.filename)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        size = 0
        try:
            with (manager.directory(identifier) / "upload.bin").open("wb") as target:
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "Размер видео превышает 250 МБ")
                    target.write(chunk)
            if size == 0:
                raise HTTPException(400, "Файл пуст")
        except Exception:
            manager.update(identifier, status="failed", error="Загрузка не завершена")
            manager.delete(identifier)
            raise
        finally:
            await file.close()
        manager.submit_analysis(identifier)
        return manager.get(identifier)

    @application.get("/api/videos/{identifier}")
    def detail(identifier: str, request: Request):
        job = get_job(request, identifier)
        if job["status"] in {"ready", "complete", "rendering"}:
            job["tracks"] = jobs(request).tracks(identifier)
        return job

    @application.delete("/api/videos/{identifier}")
    def delete(identifier: str, request: Request):
        get_job(request, identifier)
        try:
            jobs(request).delete(identifier)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"deleted": True}

    @application.post("/api/videos/{identifier}/preview")
    def frame(identifier: str, settings: PreviewRequest, request: Request):
        job = editable(request, identifier)
        tracks = jobs(request).tracks(identifier)
        try:
            settings.check_video(tracks, job["info"]["duration"])
            if settings.time > job["info"]["duration"]:
                raise ValueError("Время кадра выходит за границы видео")
            data = video.preview(
                jobs(request).directory(identifier) / "source.mp4",
                tracks,
                settings,
                job["info"],
                settings.time,
                settings.blurred,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return Response(data, media_type="image/jpeg")

    @application.post("/api/videos/{identifier}/render", status_code=202)
    def render(identifier: str, settings: Settings, request: Request):
        editable(request, identifier)
        try:
            jobs(request).submit_render(identifier, settings)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return jobs(request).get(identifier)

    @application.get("/api/videos/{identifier}/source")
    def source(identifier: str, request: Request):
        editable(request, identifier)
        return FileResponse(
            jobs(request).directory(identifier) / "source.mp4", media_type="video/mp4"
        )

    @application.get("/api/videos/{identifier}/result")
    def result(identifier: str, request: Request, download: bool = False):
        job = get_job(request, identifier)
        if job["status"] != "complete":
            raise HTTPException(409, "Результат ещё не готов")
        name = Path(job["filename"]).stem + "_blurred.mp4" if download else None
        return FileResponse(
            jobs(request).directory(identifier) / "result.mp4",
            media_type="video/mp4",
            filename=name,
        )

    application.mount("/static", StaticFiles(directory=STATIC), name="static")
    return application


app = create_app()
