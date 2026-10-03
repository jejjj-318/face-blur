"use strict";
const $ = (id) => document.getElementById(id);
const busyStates = new Set([
  "uploading",
  "queued",
  "normalizing",
  "analyzing",
  "rendering",
]);
const labels = {
  uploading: "Загрузка",
  queued: "В очереди",
  normalizing: "Подготовка видео",
  analyzing: "Поиск лиц",
  rendering: "Сохранение результата",
  ready: "Готово к редактированию",
  complete: "Результат готов",
  failed: "Ошибка обработки",
};
let job = null,
  settings = null,
  selected = null,
  adding = false,
  drag = null;
let previewTimer,
  previewController,
  previewVersion = 0,
  currentBlob = null,
  polling = false;
let renderedSignature = null;
let openingVersion = 0;
const libraryRows = new Map();
const fps = () => {
  const parts = job.info.fps.split("/").map(Number);
  return parts[0] / (parts[1] || 1);
};
const time = () => Number($("timeline").value);
const lastTime = () => (job.info.frames - 1) / fps();
const formatTime = (seconds) =>
  `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(2).padStart(5, "0")}`;
function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      const data = await response.json();
      message =
        typeof data.detail === "string"
          ? data.detail
          : "Проверьте значения параметров";
    } catch {}
    throw new Error(message);
  }
  return response;
}
function jsonOptions(value) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}
function defaultSettings(value) {
  return {
    strength: 70,
    auto: value.tracks.map((t) => ({
      id: t.id,
      enabled: true,
      dx: 0,
      dy: 0,
      sx: 1,
      sy: 1,
    })),
    manual: [],
  };
}
function hydrateSettings(value) {
  const edits = value?.auto || [];
  return {
    ...defaultSettings(job),
    ...value,
    auto: job.tracks.map(
      (t) =>
        edits.find((e) => e.id === t.id) || {
          id: t.id,
          enabled: true,
          dx: 0,
          dy: 0,
          sx: 1,
          sy: 1,
        },
    ),
  };
}
function saveEdits() {
  try {
    localStorage.setItem(`face-blur:${job.id}`, JSON.stringify(settings));
  } catch {}
}
function signature(value) {
  const canonical = (v) =>
    Array.isArray(v)
      ? v.map(canonical)
      : v && typeof v === "object"
        ? Object.fromEntries(
            Object.keys(v)
              .sort()
              .map((k) => [k, canonical(v[k])]),
          )
        : v;
  return JSON.stringify(canonical(value));
}
function changed() {
  saveEdits();
  draw();
  fillForm();
  renderZones();
  const stale = renderedSignature && signature(settings) !== renderedSignature;
  $("download").hidden = !!stale;
  $("result-note").textContent = stale
    ? "Настройки изменены. Сформируйте видео заново, чтобы скачать актуальный результат."
    : "Просмотрите результат перед скачиванием.";
  queuePreview();
}
function baseBox(track, seconds) {
  const frame = Math.min(job.info.frames - 1, Math.round(seconds * fps()));
  const index = frame - track.samples[0][0];
  return index >= 0 && index < track.samples.length
    ? track.samples[index].slice(1)
    : null;
}
function interpolate(mask, seconds) {
  const keys = mask.keyframes;
  let right = keys.findIndex((k) => k.time > seconds);
  if (right === 0) return { ...keys[0].box };
  if (right === -1) return { ...keys.at(-1).box };
  const a = keys[right - 1],
    b = keys[right],
    fraction = (seconds - a.time) / (b.time - a.time);
  return Object.fromEntries(
    ["x", "y", "w", "h"].map((k) => [
      k,
      a.box[k] + fraction * (b.box[k] - a.box[k]),
    ]),
  );
}
function selectedMask() {
  return selected?.type === "manual"
    ? settings.manual.find((m) => m.id === selected.id)
    : settings?.auto.find((m) => m.id === selected?.id);
}
function boxFor(selection, seconds = time()) {
  if (!selection) return null;
  if (selection.type === "manual") {
    const mask = settings.manual.find((m) => m.id === selection.id);
    return mask && seconds >= mask.start && seconds < mask.end
      ? interpolate(mask, seconds)
      : null;
  }
  const track = job.tracks.find((t) => t.id === selection.id),
    edit = settings.auto.find((m) => m.id === selection.id);
  const b = track && baseBox(track, seconds);
  return b && edit
    ? {
        x: b[0] + edit.dx,
        y: b[1] + edit.dy,
        w: b[2] * edit.sx,
        h: b[3] * edit.sy,
      }
    : null;
}
function allZones() {
  return [
    ...settings.auto.map((m) => ({ type: "auto", id: m.id })),
    ...settings.manual.map((m) => ({ type: "manual", id: m.id })),
  ];
}
function maskFor(selection) {
  return selection.type === "auto"
    ? settings.auto.find((m) => m.id === selection.id)
    : settings.manual.find((m) => m.id === selection.id);
}
function same(a, b) {
  return a?.type === b?.type && a?.id === b?.id;
}
function zoneLabel(selection) {
  return selection.type === "auto"
    ? `Авто ${selection.id}`
    : `Ручная ${settings.manual.findIndex((m) => m.id === selection.id) + 1}`;
}
function selectZone(selection) {
  selected = selection;
  adding = false;
  $("stage").classList.remove("adding");
  $("add").textContent = "＋ Добавить";
  if (!boxFor(selection)) {
    const seconds =
      selection.type === "auto"
        ? job.tracks.find((t) => t.id === selection.id).samples[0][0] / fps()
        : settings.manual.find((m) => m.id === selection.id).start;
    $("timeline").value = Math.min(lastTime(), seconds);
    updateTime();
    queuePreview();
  }
  renderZones();
  fillForm();
  draw();
}
function renderZones() {
  $("zones").replaceChildren();
  for (const selection of allZones()) {
    const mask = maskFor(selection),
      button = document.createElement("button");
    button.className = `zone ${selection.type} ${same(selected, selection) ? "selected" : ""} ${mask.enabled ? "" : "off"}`;
    const name = document.createElement("b"),
      status = document.createElement("span");
    name.textContent = zoneLabel(selection);
    status.textContent = mask.enabled
      ? boxFor(selection)
        ? "В кадре"
        : "Вне кадра"
      : "Выключена";
    button.append(name, status);
    button.onclick = () => selectZone(selection);
    $("zones").append(button);
  }
  $("no-zones").hidden = allZones().length > 0;
}
function fillForm() {
  const mask = selectedMask(),
    box = boxFor(selected);
  $("zone-form").hidden = !mask;
  if (!mask) return;
  $("zone-name").textContent = zoneLabel(selected);
  $("enabled").checked = mask.enabled;
  for (const key of ["x", "y", "w", "h"]) {
    const input = $(`box-${key}`);
    input.disabled = !box;
    if (document.activeElement !== input)
      input.value = box ? (box[key] * 100).toFixed(1) : "";
  }
  const manual = selected.type === "manual";
  $("manual-controls").hidden = !manual;
  $("auto-help").hidden = manual;
  $("reset-zone").hidden = manual;
  if (manual) {
    if (document.activeElement !== $("start"))
      $("start").value = mask.start.toFixed(2);
    if (document.activeElement !== $("end"))
      $("end").value = mask.end.toFixed(2);
    $("start").max = job.info.duration;
    $("end").max = job.info.duration;
    $("keyframe-count").textContent =
      `Опорные кадры: ${mask.keyframes.map((k) => formatTime(k.time)).join(", ")}`;
    $("keyframe").disabled = !box;
    $("remove-keyframe").disabled =
      mask.keyframes.length <= 1 ||
      !mask.keyframes.some((k) => Math.abs(k.time - time()) < 0.5 / fps());
  }
}
function clampBox(box) {
  const b = { ...box };
  b.w = Math.max(0.005, Math.min(1, b.w));
  b.h = Math.max(0.005, Math.min(1, b.h));
  b.x = Math.max(0, Math.min(1 - b.w, b.x));
  b.y = Math.max(0, Math.min(1 - b.h, b.y));
  return b;
}
function setBox(box) {
  const b = clampBox(box),
    mask = selectedMask();
  if (!mask) return;
  if (selected.type === "auto") {
    const base = baseBox(
      job.tracks.find((t) => t.id === selected.id),
      time(),
    );
    if (!base) return;
    mask.dx = b.x - base[0];
    mask.dy = b.y - base[1];
    mask.sx = Math.max(0.2, Math.min(5, b.w / base[2]));
    mask.sy = Math.max(0.2, Math.min(5, b.h / base[3]));
  } else if (mask.keyframes.length === 1) mask.keyframes[0].box = b;
  else {
    const key = mask.keyframes.find(
      (k) => Math.abs(k.time - time()) < 0.5 / fps(),
    );
    if (key) key.box = b;
    else {
      if (mask.keyframes.length >= 100) {
        notice("В одной зоне может быть до 100 опорных кадров.");
        return;
      }
      mask.keyframes.push({ time: time(), box: b });
      mask.keyframes.sort((a, b) => a.time - b.time);
    }
  }
}
function draw() {
  if (!job?.tracks || !settings) return;
  const canvas = $("canvas"),
    bounds = $("stage").getBoundingClientRect(),
    ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(bounds.width * ratio);
  canvas.height = Math.round(bounds.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const zones = allZones();
  if (drag?.mode === "add") zones.push({ type: "draft", id: "draft" });
  for (const selection of zones) {
    const draft = selection.type === "draft",
      box = draft ? drag.box : boxFor(selection),
      mask = draft ? { enabled: true } : maskFor(selection);
    if (!box || !mask.enabled) continue;
    const active = same(selected, selection) || draft,
      color = selection.type === "auto" ? "#40d8c4" : "#ffb260";
    const x = box.x * bounds.width,
      y = box.y * bounds.height,
      w = box.w * bounds.width,
      h = box.h * bounds.height;
    ctx.strokeStyle = color;
    ctx.lineWidth = active ? 2.5 : 1.3;
    ctx.setLineDash(active ? [] : [5, 4]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
    if (active) {
      ctx.fillStyle = color;
      ctx.fillRect(x + w - 5, y + h - 5, 10, 10);
    }
    const label = draft ? "Новая зона" : zoneLabel(selection);
    ctx.font = "11px Segoe UI";
    const labelWidth = ctx.measureText(label).width + 12,
      labelY = Math.max(0, y - 20),
      labelX = Math.max(0, Math.min(bounds.width - labelWidth, x));
    ctx.fillStyle = "#153440e0";
    ctx.fillRect(labelX, labelY, labelWidth, 18);
    ctx.fillStyle = color;
    ctx.fillText(label, labelX + 6, labelY + 13);
  }
}
function point(event) {
  const r = $("stage").getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - r.left) / r.width)),
    y: Math.max(0, Math.min(1, (event.clientY - r.top) / r.height)),
  };
}
$("canvas").addEventListener("pointerdown", (event) => {
  if (!settings || event.button !== 0) return;
  const p = point(event);
  $("canvas").setPointerCapture(event.pointerId);
  if (adding) {
    drag = { mode: "add", start: p, box: { x: p.x, y: p.y, w: 0, h: 0 } };
    draw();
    return;
  }
  let hit = null,
    mode = "move";
  const chosen = boxFor(selected),
    bounds = $("stage").getBoundingClientRect();
  if (
    chosen &&
    Math.abs(p.x - chosen.x - chosen.w) * bounds.width < 14 &&
    Math.abs(p.y - chosen.y - chosen.h) * bounds.height < 14
  ) {
    hit = selected;
    mode = "resize";
  }
  if (!hit)
    hit = allZones()
      .reverse()
      .find((s) => {
        const b = boxFor(s);
        return (
          maskFor(s).enabled &&
          b &&
          p.x >= b.x &&
          p.x <= b.x + b.w &&
          p.y >= b.y &&
          p.y <= b.y + b.h
        );
      });
  if (hit) {
    selectZone(hit);
    drag = { mode, start: p, original: { ...boxFor(hit) } };
  }
});
$("canvas").addEventListener("pointermove", (event) => {
  if (!drag) return;
  const p = point(event),
    dx = p.x - drag.start.x,
    dy = p.y - drag.start.y;
  if (drag.mode === "add")
    drag.box = {
      x: Math.min(p.x, drag.start.x),
      y: Math.min(p.y, drag.start.y),
      w: Math.abs(dx),
      h: Math.abs(dy),
    };
  else {
    const b = { ...drag.original };
    if (drag.mode === "resize") {
      b.w += dx;
      b.h += dy;
    } else {
      b.x += dx;
      b.y += dy;
    }
    setBox(b);
    fillForm();
  }
  draw();
});
function endDrag() {
  if (!drag) return;
  if (drag.mode === "add" && drag.box.w >= 0.005 && drag.box.h >= 0.005) {
    if (settings.manual.length >= 200) {
      drag = null;
      notice("Можно добавить до 200 ручных зон.");
      draw();
      return;
    }
    const mask = {
      id: `m_${Date.now()}`,
      enabled: true,
      start: 0,
      end: job.info.duration,
      keyframes: [{ time: 0, box: clampBox(drag.box) }],
    };
    settings.manual.push(mask);
    selected = { type: "manual", id: mask.id };
    adding = false;
    $("add").textContent = "＋ Добавить";
    $("stage").classList.remove("adding");
  }
  drag = null;
  changed();
}
$("canvas").addEventListener("pointerup", endDrag);
$("canvas").addEventListener("pointercancel", endDrag);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    drag = null;
    adding = false;
    $("add").textContent = "＋ Добавить";
    draw();
  }
});
new ResizeObserver(draw).observe($("stage"));
function updateTime() {
  $("time").textContent = formatTime(time());
  fillForm();
  renderZones();
  draw();
}
function queuePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 130);
}
async function refreshPreview() {
  if (!job || !settings || busyStates.has(job.status)) return;
  const version = ++previewVersion;
  previewController?.abort();
  previewController = new AbortController();
  $("frame-loading").hidden = false;
  try {
    const response = await api(`/api/videos/${job.id}/preview`, {
      ...jsonOptions({
        ...settings,
        time: time(),
        blurred: $("blur-preview").checked,
      }),
      signal: previewController.signal,
    });
    const blob = await response.blob();
    if (version !== previewVersion) return;
    if (currentBlob) URL.revokeObjectURL(currentBlob);
    currentBlob = URL.createObjectURL(blob);
    $("frame").src = currentBlob;
  } catch (error) {
    if (error.name !== "AbortError") notice(error.message);
  } finally {
    if (version === previewVersion) $("frame-loading").hidden = true;
  }
}
$("frame").onload = draw;
$("timeline").oninput = () => {
  updateTime();
  queuePreview();
};
for (const [id, direction] of [
  ["previous", -1],
  ["next", 1],
])
  $(id).onclick = () => {
    $("timeline").value = Math.max(
      0,
      Math.min(lastTime(), time() + direction / fps()),
    );
    updateTime();
    queuePreview();
  };
$("blur-preview").onchange = () => {
  $("frame-mode").textContent = $("blur-preview").checked
    ? "Кадр с размытием"
    : "Исходный кадр";
  queuePreview();
};
$("strength").oninput = () => {
  settings.strength = Number($("strength").value);
  $("strength-value").textContent = `${settings.strength}%`;
  changed();
};
$("add").onclick = () => {
  adding = !adding;
  $("add").textContent = adding ? "Отменить" : "＋ Добавить";
  $("stage").classList.toggle("adding", adding);
};
$("enabled").onchange = () => {
  selectedMask().enabled = $("enabled").checked;
  changed();
};
function applyGeometry() {
  if (["x", "y", "w", "h"].some((k) => $(`box-${k}`).value === "")) return;
  const box = Object.fromEntries(
    ["x", "y", "w", "h"].map((k) => [k, Number($(`box-${k}`).value) / 100]),
  );
  if (Object.values(box).every(Number.isFinite)) {
    setBox(box);
    changed();
  }
}
for (const key of ["x", "y", "w", "h"]) {
  $(`box-${key}`).oninput = applyGeometry;
  $(`box-${key}`).onchange = applyGeometry;
  $(`box-${key}`).onblur = fillForm;
}
$("reset-zone").onclick = () => {
  Object.assign(selectedMask(), { dx: 0, dy: 0, sx: 1, sy: 1 });
  changed();
};
$("remove-zone").onclick = () => {
  settings.manual = settings.manual.filter((m) => m.id !== selected.id);
  selected = null;
  changed();
};
$("keyframe").onclick = () => {
  const mask = selectedMask(),
    t = time();
  if (t < mask.start || t >= mask.end) return;
  if (!mask.keyframes.some((k) => Math.abs(k.time - t) < 0.5 / fps())) {
    if (mask.keyframes.length >= 100) {
      notice("В одной зоне может быть до 100 опорных кадров.");
      return;
    }
    mask.keyframes.push({ time: t, box: interpolate(mask, t) });
    mask.keyframes.sort((a, b) => a.time - b.time);
    changed();
  }
};
$("remove-keyframe").onclick = () => {
  const mask = selectedMask();
  if (mask.keyframes.length > 1) {
    mask.keyframes = mask.keyframes.filter(
      (k) => Math.abs(k.time - time()) >= 0.5 / fps(),
    );
    changed();
  }
};
function applyInterval() {
  if (selected?.type !== "manual") return true;
  const mask = selectedMask(),
    start = Number($("start").value),
    enteredEnd = Number($("end").value),
    end =
      Math.abs(enteredEnd - job.info.duration) < 0.01
        ? job.info.duration
        : enteredEnd;
  if (
    $("start").value === "" ||
    $("end").value === "" ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    end <= start ||
    end > job.info.duration
  ) {
    notice("Задайте интервал внутри видео: конец должен быть позже начала.");
    return false;
  }
  if (start === mask.start && end === mask.end) return true;
  const box = interpolate(mask, start),
    endBox = interpolate(mask, end),
    oldKeys = mask.keyframes;
  mask.start = start;
  mask.end = end;
  mask.keyframes = oldKeys.filter((k) => k.time >= start && k.time <= end);
  if (oldKeys.length > 1) {
    if (
      oldKeys[0].time < start &&
      !mask.keyframes.some((k) => k.time === start)
    )
      mask.keyframes.unshift({ time: start, box });
    if (
      oldKeys.at(-1).time > end &&
      !mask.keyframes.some((k) => k.time === end)
    )
      mask.keyframes.push({ time: end, box: endBox });
  }
  if (!mask.keyframes.length) mask.keyframes = [{ time: start, box }];
  notice();
  changed();
  return true;
}
for (const id of ["start", "end"]) {
  $(id).onchange = applyInterval;
  $(id).onblur = applyInterval;
}
$("render").onclick = async () => {
  if (!applyInterval()) return;
  notice();
  $("render").disabled = true;
  try {
    job = await (
      await api(`/api/videos/${job.id}/render`, jsonOptions(settings))
    ).json();
    showState();
  } catch (error) {
    notice(error.message);
  } finally {
    $("render").disabled = false;
  }
};
function showState() {
  const busy = job && busyStates.has(job.status),
    failed = job?.status === "failed";
  $("empty").hidden = !!job;
  $("processing").hidden = !busy;
  $("editor").hidden = !job || busy || failed;
  $("result").hidden = !job || busy || job.status !== "complete";
  if (busy) {
    $("process-title").textContent = labels[job.status];
    $("progress").value = job.progress;
    $("percent").textContent = `${job.progress}%`;
    $("process-description").textContent =
      job.status === "analyzing"
        ? "Проверяем каждый кадр и связываем зоны между кадрами."
        : job.status === "rendering"
          ? "Применяем маски и сохраняем видео. Это может занять несколько минут."
          : "Подготавливаем совместимое видео для просмотра в браузере.";
  }
  if (failed) {
    $("empty").hidden = false;
    notice(job.error);
  }
}
async function openJob(identifier) {
  const version = ++openingVersion;
  previewController?.abort();
  ++previewVersion;
  notice();
  const value = await (await api(`/api/videos/${identifier}`)).json();
  if (version !== openingVersion) return;
  job = value;
  selected = null;
  adding = false;
  drag = null;
  $("add").textContent = "＋ Добавить";
  showState();
  if (!busyStates.has(job.status) && job.status !== "failed") {
    settings = hydrateSettings(job.settings);
    try {
      const saved = JSON.parse(localStorage.getItem(`face-blur:${job.id}`));
      if (saved && Array.isArray(saved.auto) && Array.isArray(saved.manual))
        settings = hydrateSettings(saved);
    } catch {}
    renderedSignature =
      job.status === "complete"
        ? signature(hydrateSettings(job.settings))
        : null;
    $("filename").textContent = job.filename;
    $("metadata").textContent =
      `${job.info.width} × ${job.info.height} · ${formatTime(job.info.duration)} · ${fps().toFixed(2)} кадр/с${job.info.audio ? " · Со звуком" : " · Без звука"}`;
    $("detected").textContent = `Автозон: ${job.track_count}`;
    $("strength").value = settings.strength;
    $("strength-value").textContent = `${settings.strength}%`;
    $("timeline").max = lastTime();
    $("timeline").step = 1 / fps();
    $("timeline").value = 0;
    $("frame").style.aspectRatio = `${job.info.width}/${job.info.height}`;
    $("source").src = `/api/videos/${job.id}/source`;
    if (job.status === "complete") {
      $("result-video").src = `/api/videos/${job.id}/result?v=${Date.now()}`;
      $("download").href = `/api/videos/${job.id}/result?download=true`;
    }
    if (job.error) notice(job.error);
    updateTime();
    changed();
  }
  await refreshLibrary();
}
async function refreshLibrary() {
  const videos = await (await api("/api/videos")).json();
  const ids = new Set(videos.map((v) => v.id));
  for (const [id, row] of libraryRows) {
    if (!ids.has(id)) {
      row.remove();
      libraryRows.delete(id);
    }
  }
  for (const item of videos) {
    if (libraryRows.has(item.id)) {
      const row = libraryRows.get(item.id);
      row.classList.toggle("active", item.id === job?.id);
      row.querySelector("span").textContent =
        labels[item.status] +
        (busyStates.has(item.status) ? ` · ${item.progress}%` : "");
      row.querySelector(".delete-video").disabled = busyStates.has(item.status);
      continue;
    }
    const row = document.createElement("div"),
      button = document.createElement("button"),
      title = document.createElement("strong"),
      state = document.createElement("span"),
      remove = document.createElement("button");
    row.className = `video-row ${item.id === job?.id ? "active" : ""}`;
    button.className = "video-select";
    title.textContent = item.filename;
    state.textContent =
      labels[item.status] +
      (busyStates.has(item.status) ? ` · ${item.progress}%` : "");
    button.append(title, state);
    button.onclick = () => openJob(item.id).catch((e) => notice(e.message));
    remove.className = "delete-video";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Удалить ${item.filename}`);
    remove.disabled = busyStates.has(item.status);
    remove.onclick = async () => {
      if (
        !window.confirm(
          "Удалить загруженную копию видео и результат? Исходный файл на устройстве останется.",
        )
      )
        return;
      try {
        await api(`/api/videos/${item.id}`, { method: "DELETE" });
        localStorage.removeItem(`face-blur:${item.id}`);
        if (job?.id === item.id) {
          job = null;
          settings = null;
          selected = null;
          $("source").removeAttribute("src");
          $("result-video").removeAttribute("src");
          showState();
          notice();
        }
        await refreshLibrary();
      } catch (error) {
        notice(error.message);
      }
    };
    row.append(button, remove);
    libraryRows.set(item.id, row);
    $("library").append(row);
  }
  videos.forEach((item, index) => {
    const row = libraryRows.get(item.id);
    if ($("library").children[index] !== row)
      $("library").insertBefore(row, $("library").children[index] || null);
  });
  return videos;
}
$("upload").onclick = () => $("file").click();
$("file").onchange = () => {
  const file = $("file").files[0];
  if (!file) return;
  $("file").value = "";
  notice();
  if (file.size > 250 * 1024 ** 2) {
    notice("Размер видео превышает 250 МБ.");
    return;
  }
  $("upload").disabled = true;
  $("upload-state").hidden = false;
  const xhr = new XMLHttpRequest(),
    body = new FormData();
  body.append("file", file);
  xhr.open("POST", "/api/videos");
  xhr.upload.onprogress = (event) => {
    const percent = event.lengthComputable
      ? Math.round((100 * event.loaded) / event.total)
      : 0;
    $("upload-state").querySelector("span").textContent =
      `Загрузка: ${percent}%`;
    $("upload-state").querySelector("progress").value = percent;
  };
  xhr.onload = () => {
    try {
      const data = JSON.parse(xhr.responseText);
      if (xhr.status >= 400)
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Не удалось загрузить видео",
        );
      openJob(data.id).catch((e) => notice(e.message));
    } catch (error) {
      notice(error.message);
    }
  };
  xhr.onerror = () =>
    notice("Соединение прервалось. Проверьте, запущено ли приложение.");
  xhr.onloadend = () => {
    $("upload").disabled = false;
    $("upload-state").hidden = true;
  };
  xhr.send(body);
};
setInterval(async () => {
  if (polling) return;
  polling = true;
  try {
    const list = await refreshLibrary();
    if (job && busyStates.has(job.status)) {
      const next = list.find((j) => j.id === job.id);
      if (next && !busyStates.has(next.status)) await openJob(job.id);
      else if (next) {
        job = { ...job, ...next };
        showState();
      }
    }
  } catch (error) {
    notice(error.message);
  } finally {
    polling = false;
  }
}, 1500);
refreshLibrary().catch((error) => notice(error.message));
