"""Геометрия масок: общая для предпросмотра кадра и итогового видео."""

import bisect
import math

import cv2
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Box(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0.005, le=1)
    h: float = Field(ge=0.005, le=1)

    @model_validator(mode="after")
    def inside(self):
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError("Зона выходит за границы кадра")
        return self


class Keyframe(StrictModel):
    time: float = Field(ge=0)
    box: Box


class AutoEdit(StrictModel):
    id: int = Field(ge=1)
    enabled: bool = True
    dx: float = Field(default=0, ge=-1, le=1)
    dy: float = Field(default=0, ge=-1, le=1)
    sx: float = Field(default=1, ge=0.2, le=5)
    sy: float = Field(default=1, ge=0.2, le=5)


class ManualMask(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    enabled: bool = True
    keyframes: list[Keyframe] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered(self):
        times = [k.time for k in self.keyframes]
        if self.end <= self.start or times != sorted(set(times)):
            raise ValueError("Неверный интервал или порядок опорных кадров")
        if any(t < self.start or t > self.end for t in times):
            raise ValueError("Опорный кадр должен находиться в интервале зоны")
        return self


class Settings(StrictModel):
    strength: int = Field(default=70, ge=10, le=100)
    auto: list[AutoEdit] = Field(default_factory=list, max_length=1000)
    manual: list[ManualMask] = Field(default_factory=list, max_length=200)

    def check_video(self, tracks, duration):
        known = {track["id"] for track in tracks}
        ids = [edit.id for edit in self.auto]
        if len(ids) != len(set(ids)) or not set(ids) <= known:
            raise ValueError("Неизвестная или повторная автоматическая зона")
        manual_ids = [mask.id for mask in self.manual]
        if len(manual_ids) != len(set(manual_ids)):
            raise ValueError("Повторные ручные зоны")
        if any(mask.end > duration + 0.001 for mask in self.manual):
            raise ValueError("Интервал зоны превышает длительность видео")


class PreviewRequest(Settings):
    time: float = Field(ge=0)
    blurred: bool = True


def iou(a, b):
    x = max(a[0], b[0])
    y = max(a[1], b[1])
    overlap = max(0, min(a[0] + a[2], b[0] + b[2]) - x) * max(
        0, min(a[1] + a[3], b[1] + b[3]) - y
    )
    union = a[2] * a[3] + b[2] * b[3] - overlap
    return overlap / union if union > 0 else 0


class Tracker:
    """Сопоставляет прямоугольники по IoU; это не распознавание личности."""

    def __init__(self, hold_frames):
        self.hold_frames = hold_frames
        self.tracks = []
        self.active = []

    def update(self, frame, boxes):
        candidates = []
        for track in self.active:
            if frame - track["last_seen"] > self.hold_frames + 1:
                continue
            for index, box in enumerate(boxes):
                score = iou(track["last_box"], box)
                if score >= 0.15:
                    candidates.append((score, track["id"], index))
        assigned_tracks, assigned_boxes = set(), set()
        for _, track_id, index in sorted(candidates, reverse=True):
            if track_id in assigned_tracks or index in assigned_boxes:
                continue
            track = self.tracks[track_id - 1]
            track["last_box"] = boxes[index]
            track["last_seen"] = frame
            assigned_tracks.add(track_id)
            assigned_boxes.add(index)
        for index, box in enumerate(boxes):
            if index not in assigned_boxes:
                track = {
                    "id": len(self.tracks) + 1,
                    "last_seen": frame,
                    "last_box": box,
                    "samples": [],
                }
                self.tracks.append(track)
                self.active.append(track)
        self.active = [
            t for t in self.active if frame - t["last_seen"] <= self.hold_frames
        ]
        for track in self.active:
            track["samples"].append([frame, *track["last_box"]])

    def result(self):
        return [{"id": t["id"], "samples": t["samples"]} for t in self.tracks]


def manual_box(mask, time):
    times = [k.time for k in mask.keyframes]
    right = bisect.bisect_right(times, time)
    if right == 0:
        return list(mask.keyframes[0].box.model_dump().values())
    if right == len(times):
        return list(mask.keyframes[-1].box.model_dump().values())
    left_key, right_key = mask.keyframes[right - 1], mask.keyframes[right]
    alpha = (time - left_key.time) / (right_key.time - left_key.time)
    a = list(left_key.box.model_dump().values())
    b = list(right_key.box.model_dump().values())
    return [a[i] + alpha * (b[i] - a[i]) for i in range(4)]


class MaskPlan:
    """Индекс готовится один раз, а не заново для каждого кадра."""

    def __init__(self, tracks, settings, fps):
        self.fps = fps
        self.manual = settings.manual
        edits = {edit.id: edit for edit in settings.auto}
        self.auto = []
        for track in tracks:
            edit = edits.get(track["id"], AutoEdit(id=track["id"]))
            if edit.enabled:
                self.auto.append((track["samples"], edit))

    def boxes(self, frame):
        boxes = []
        for samples, edit in self.auto:
            index = frame - samples[0][0]
            if 0 <= index < len(samples):
                _, x, y, w, h = samples[index]
                boxes.append([x + edit.dx, y + edit.dy, w * edit.sx, h * edit.sy])
        time = frame / self.fps
        for mask in self.manual:
            if mask.enabled and mask.start <= time < mask.end:
                boxes.append(manual_box(mask, time))
        return boxes


def blur_frame(frame, boxes, strength):
    height, width = frame.shape[:2]
    # В пересечении последней применяется зона с более сильным размытием.
    source = frame.copy()
    regions = []
    for x, y, w, h in boxes:
        x0 = max(0, min(width, math.floor(x * width)))
        y0 = max(0, min(height, math.floor(y * height)))
        x1 = max(0, min(width, math.ceil((x + w) * width)))
        y1 = max(0, min(height, math.ceil((y + h) * height)))
        if x1 <= x0 or y1 <= y0:
            continue
        sigma = max(1, min(x1 - x0, y1 - y0) * strength / 400)
        regions.append((sigma, x0, y0, x1, y1))
    for sigma, x0, y0, x1, y1 in sorted(regions):
        region = source[y0:y1, x0:x1]
        # При большом sigma высокие частоты всё равно подавляются. Сначала
        # усредняем область, размываем в меньшем масштабе и плавно увеличиваем.
        factor = max(1, math.ceil(sigma / 6))
        small_width = max(1, math.ceil((x1 - x0) / factor))
        small_height = max(1, math.ceil((y1 - y0) / factor))
        small = cv2.resize(
            region, (small_width, small_height), interpolation=cv2.INTER_AREA
        )
        sigma_x = sigma * small_width / (x1 - x0)
        sigma_y = sigma * small_height / (y1 - y0)
        kernel = (2 * math.ceil(3 * sigma_x) + 1, 2 * math.ceil(3 * sigma_y) + 1)
        blurred = cv2.GaussianBlur(
            small, kernel, sigma_x, sigmaY=sigma_y, borderType=cv2.BORDER_REPLICATE
        )
        frame[y0:y1, x0:x1] = cv2.resize(
            blurred, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR
        )
    return frame
