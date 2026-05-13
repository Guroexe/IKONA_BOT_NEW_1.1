# -*- coding: utf-8 -*-
"""
Генерация изображений: SDXL (Gradio/A1111 API), OpenRouter, Polza AI, очередь до 50 задач.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import re
import time
from collections import deque
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# --- Параметры SDXL (фиксированные) ---
SDXL_STEPS = 5
SDXL_CFG = 1.5
SDXL_WIDTH = 784
SDXL_HEIGHT = 1248
SDXL_SAMPLER = "DPM++ SDE"
SDXL_SCHEDULER = "Karras"

SDXL_TIMEOUT = 60.0
API_IMAGE_TIMEOUT = 180.0
MAX_QUEUE_LEN = 50

GEN_MODE_SDXL = "sdxl"
GEN_MODE_OPENROUTER = "openrouter"
GEN_MODE_POLZA = "polza"

# Модели по умолчанию (можно заменить в настройках позже)
OPENROUTER_IMAGE_MODEL = "openai/gpt-image-1"
# База для GET /v1/media/{id} (опрос статуса)
POLZA_IMAGE_BASE = "https://api.polza.ai/v1"
# Media API (model вида openai/…), как в личном кабинете Polza
POLZA_MEDIA_POST_URLS = (
    "https://polza.ai/api/v1/media",
    "https://api.polza.ai/v1/media",
)
# ChatGPT Image 2 на Polza (пример с сайта polza.ai)
POLZA_MEDIA_MODEL_DEFAULT = "openai/gpt-5.4-image-2"
# Соотношение сторон для этой модели (допустимые значения — в доке Polza для GPT Image)
# Соотношение по умолчанию для txt2img (без референса): портрет, не «привязка» к старому фото
POLZA_MEDIA_ASPECT_RATIO_TXT2IMG_DEFAULT = "9:16"
# Fallback, если не удалось прочитать размеры референса
POLZA_MEDIA_ASPECT_RATIO_DEFAULT = "16:9"
# OpenAI-совместимый /v2/images/* — только «короткие» имена, доступные ключу (часто dall-e-3)
POLZA_IMAGE_GENERATIONS_URL = "https://api.polza.ai/v2/images/generations"
POLZA_IMAGE_EDITS_URL = "https://api.polza.ai/v2/images/edits"
POLZA_V2_FALLBACK_MODEL = "dall-e-3"
# Альтернативный хост из документации (если GET на api.polza.ai не найден)
POLZA_MEDIA_POLL_FALLBACK = "https://polza.ai/api/v1"
POLZA_POLL_INTERVAL_SEC = 3.0
# OpenAI-совместимый список моделей — GET с Bearer для проверки ключа (док. Polza)
POLZA_OPENAI_COMPAT_MODELS_URL = "https://polza.ai/api/v1/models"


class PolzaUnauthorized(RuntimeError):
    """401 от Polza Media / совместимого API — ключ не принят."""


# Блокировки по chat_id (не кладём Lock в pickle user_data)
_GEN_LOCKS: dict[int, asyncio.Lock] = {}


def _lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _GEN_LOCKS:
        _GEN_LOCKS[chat_id] = asyncio.Lock()
    return _GEN_LOCKS[chat_id]


def get_gen_settings(ud: dict) -> dict:
    g = ud.get("gen_settings")
    if not isinstance(g, dict):
        g = {}
        ud["gen_settings"] = g
    g.setdefault("denoise", 0.65)
    return g


def normalize_stored_api_key(text: str) -> str:
    """Убирает пробелы по краям и парные кавычки из вставленного API-ключа."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return t


async def verify_polza_api_key(client: httpx.AsyncClient, api_key: str) -> Optional[str]:
    """
    Лёгкий GET списка моделей с Bearer. При 401 возвращает 'invalid_key';
    при успехе или сетевой ошибке — None (ключ не отклоняем на сохранении).
    """
    key = normalize_stored_api_key(api_key)
    if len(key) < 8:
        return "invalid_key"
    try:
        r = await client.get(
            POLZA_OPENAI_COMPAT_MODELS_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=20.0,
        )
        if r.status_code == 401:
            return "invalid_key"
    except Exception:
        pass
    return None


def is_generation_configured(ud: dict) -> bool:
    gs = get_gen_settings(ud)
    mode = gs.get("mode")
    if mode == GEN_MODE_SDXL:
        return bool(gs.get("gradio_url"))
    if mode in (GEN_MODE_OPENROUTER, GEN_MODE_POLZA):
        return bool(gs.get("api_key"))
    return False


def normalize_gradio_base(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    u = u.split("?")[0].rstrip("/")
    return u


def _sd_api_txt2img_payload(prompt: str) -> dict:
    return {
        "prompt": prompt,
        "negative_prompt": "",
        "steps": SDXL_STEPS,
        "width": SDXL_WIDTH,
        "height": SDXL_HEIGHT,
        "cfg_scale": SDXL_CFG,
        "sampler_name": SDXL_SAMPLER,
        "scheduler": SDXL_SCHEDULER,
        "batch_size": 1,
        "n_iter": 1,
        "restore_faces": False,
    }


def _sd_api_img2img_payload(prompt: str, init_b64: str, denoise: float) -> dict:
    p = _sd_api_txt2img_payload(prompt)
    p["init_images"] = [init_b64]
    p["denoising_strength"] = max(0.0, min(1.0, float(denoise)))
    return p


async def _sdxl_txt2img_http(client: httpx.AsyncClient, base: str, prompt: str) -> bytes:
    url = f"{base}/sdapi/v1/txt2img"
    r = await client.post(url, json=_sd_api_txt2img_payload(prompt), timeout=SDXL_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    images = data.get("images") or []
    if not images:
        raise RuntimeError("SD API: пустой ответ images")
    return base64.b64decode(images[0])


async def _sdxl_img2img_http(
    client: httpx.AsyncClient, base: str, prompt: str, init_png_bytes: bytes, denoise: float
) -> bytes:
    url = f"{base}/sdapi/v1/img2img"
    b64 = base64.b64encode(init_png_bytes).decode("ascii")
    payload = _sd_api_img2img_payload(prompt, b64, denoise)
    r = await client.post(url, json=payload, timeout=SDXL_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    images = data.get("images") or []
    if not images:
        raise RuntimeError("SD API img2img: пустой ответ images")
    return base64.b64decode(images[0])


async def _openrouter_image(
    client: httpx.AsyncClient,
    api_key: str,
    prompt: str,
    init_bytes: Optional[bytes] = None,
) -> bytes:
    api_key = normalize_stored_api_key(api_key)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://t.me/",
        "X-Title": "IKONA Bot",
    }
    # 1) Пробуем OpenAI-совместимые изображения
    img_url = "https://openrouter.ai/api/v1/images/generations"
    body = {
        "model": OPENROUTER_IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    try:
        r = await client.post(img_url, headers=headers, json=body, timeout=API_IMAGE_TIMEOUT)
        if r.status_code == 200:
            js = r.json()
            arr = js.get("data") or []
            if arr and arr[0].get("b64_json"):
                return base64.b64decode(arr[0]["b64_json"])
            if arr and arr[0].get("url"):
                ir = await client.get(arr[0]["url"], timeout=60.0)
                ir.raise_for_status()
                return ir.content
    except Exception as e:
        logger.info(f"OpenRouter /images/generations: {e}")

    # 2) Редактирование / variations с изображением (если есть)
    if init_bytes:
        edit_url = "https://openrouter.ai/api/v1/images/edits"
        try:
            files = {
                "image": ("input.png", init_bytes, "image/png"),
                "model": (None, OPENROUTER_IMAGE_MODEL),
                "prompt": (None, prompt),
                "n": (None, "1"),
                "size": (None, "1024x1024"),
            }
            r = await client.post(edit_url, headers=headers, files=files, timeout=API_IMAGE_TIMEOUT)
            if r.status_code == 200:
                js = r.json()
                arr = js.get("data") or []
                if arr and arr[0].get("b64_json"):
                    return base64.b64decode(arr[0]["b64_json"])
        except Exception as e:
            logger.info(f"OpenRouter /images/edits: {e}")

    raise RuntimeError(
        "OpenRouter: не удалось получить изображение. Проверьте ключ и модель "
        f"({OPENROUTER_IMAGE_MODEL}), либо поддержку endpoints /images/generations и /images/edits."
    )


async def _polza_extract_image_bytes(client: httpx.AsyncClient, r: httpx.Response) -> Optional[bytes]:
    """Разбор ответа Polza / OpenAI-совместимого API (200 и 201)."""
    if r.status_code not in (200, 201):
        return None
    try:
        js = r.json()
    except Exception:
        logger.info("Polza: не JSON в ответе, фрагмент: %s", r.text[:400])
        return None

    data = js.get("data")
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, dict):
            if row.get("b64_json"):
                return base64.b64decode(row["b64_json"])
            if row.get("url"):
                ir = await client.get(row["url"], timeout=60.0)
                ir.raise_for_status()
                return ir.content

    for key in ("image", "image_base64", "b64_json", "result", "output", "url"):
        v = js.get(key)
        if not v or not isinstance(v, str):
            continue
        if v.startswith("http"):
            ir = await client.get(v, timeout=60.0)
            ir.raise_for_status()
            return ir.content
        try:
            return base64.b64decode(v)
        except Exception:
            continue

    logger.info("Polza: неизвестная форма ответа: %s", str(js)[:800])
    return None


def _polza_async_job_id(js: dict) -> Optional[str]:
    """ID задачи: Media API отдаёт id, v2 images — requestId."""
    return (
        js.get("requestId")
        or js.get("request_id")
        or js.get("task_id")
        or js.get("taskId")
        or js.get("id")
    )


# Пресеты соотношения сторон (ширина:высота) для Polza GPT Image / Media API
_POLZA_ASPECT_PRESETS: tuple[tuple[str, float], ...] = (
    ("21:9", 21 / 9),
    ("16:9", 16 / 9),
    ("3:2", 3 / 2),
    ("4:3", 4 / 3),
    ("5:4", 5 / 4),
    ("1:1", 1.0),
    ("4:5", 4 / 5),
    ("3:4", 3 / 4),
    ("2:3", 2 / 3),
    ("9:16", 9 / 16),
)


def _read_png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    if data[12:16] != b"IHDR":
        return 0, 0
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    if w <= 0 or h <= 0:
        return 0, 0
    return w, h


def _read_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return 0, 0
    i = 2
    n = len(data)
    while i < n - 8:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5):
            if i + 9 > n:
                break
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            if w > 0 and h > 0:
                return w, h
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0x01) or marker == 0x00:
            i += 2
            continue
        if marker == 0xD9:
            break
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if seg_len < 2 or i + 2 + seg_len > n:
            break
        i += 2 + seg_len
    return 0, 0


def _read_image_dimensions(data: bytes) -> tuple[int, int]:
    if not data or len(data) < 3:
        return 0, 0
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _read_png_dimensions(data)
    if data[:2] == b"\xff\xd8":
        return _read_jpeg_dimensions(data)
    return 0, 0


def _closest_polza_aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return POLZA_MEDIA_ASPECT_RATIO_DEFAULT
    r = width / height
    best_name, best_val = _POLZA_ASPECT_PRESETS[0]
    best_dist = abs(math.log(r / best_val))
    for name, val in _POLZA_ASPECT_PRESETS[1:]:
        d = abs(math.log(r / val))
        if d < best_dist:
            best_dist, best_name, best_val = d, name, val
    return best_name


def _polza_media_input_payload(
    model: str,
    prompt: str,
    init_bytes: Optional[bytes],
    aspect_ratio: Optional[str] = None,
) -> dict[str, Any]:
    """Тело поля input для POST /v1/media (Polza)."""
    inp: dict[str, Any] = {"prompt": prompt}
    if init_bytes:
        b64 = base64.b64encode(init_bytes).decode("ascii")
        inp["images"] = [{"type": "base64", "data": f"data:image/png;base64,{b64}"}]
    else:
        inp["images"] = []

    if "gpt-5.4-image-2" in model or "gpt-5.4-image" in model:
        ar = (aspect_ratio or "").strip()
        if ar:
            inp["aspect_ratio"] = ar
        elif init_bytes:
            w, h = _read_image_dimensions(init_bytes)
            if w > 0 and h > 0:
                inp["aspect_ratio"] = _closest_polza_aspect_ratio(w, h)
            else:
                inp["aspect_ratio"] = POLZA_MEDIA_ASPECT_RATIO_DEFAULT
        else:
            # txt2img: не привязываем к старому референсу — отдельный портретный дефолт
            inp["aspect_ratio"] = POLZA_MEDIA_ASPECT_RATIO_TXT2IMG_DEFAULT
    elif "gpt-image-1.5" in model:
        inp["aspect_ratio"] = "1:1"
        inp["quality"] = "high"
    elif "gemini" in model and "image" in model:
        inp.setdefault("aspect_ratio", "1:1")

    return inp


async def _polza_media_create_and_wait(
    client: httpx.AsyncClient,
    headers: dict,
    base: str,
    model: str,
    prompt: str,
    init_bytes: Optional[bytes],
    max_wait: float,
    aspect_ratio: Optional[str] = None,
) -> bytes:
    payload = {
        "model": model,
        "input": _polza_media_input_payload(model, prompt, init_bytes, aspect_ratio),
        "async": True,
    }
    last_err: Optional[str] = None
    for url in POLZA_MEDIA_POST_URLS:
        try:
            r = await client.post(url, headers=headers, json=payload, timeout=120.0)
            if r.status_code >= 400:
                last_err = f"{r.status_code} {r.text[:600]}"
                logger.info("Polza Media POST %s → %s", url, last_err)
                if r.status_code == 401:
                    raise PolzaUnauthorized(last_err)
                continue
            js = r.json()
            st = (js.get("status") or "").lower()
            mid = _polza_async_job_id(js)
            if st == "completed":
                img = await _polza_bytes_from_media_status(client, js)
                if img:
                    return img
                raise RuntimeError(f"Polza Media: completed без картинки: {repr(js)[:400]}")
            if mid:
                logger.info("Polza Media: задача %s (%s), опрос…", mid, st or "pending")
                return await _polza_poll_media_until_image(client, headers, base, mid, max_wait)
            raise RuntimeError(f"Polza Media: неожиданный ответ: {repr(js)[:400]}")
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            logger.info("Polza Media POST %s: %s", url, e)

    raise RuntimeError(f"Polza Media API: не удалось создать задачу. {last_err or 'unknown'}")


async def _polza_openai_v2_image(
    client: httpx.AsyncClient,
    headers: dict,
    base: str,
    model: str,
    prompt: str,
    init_bytes: Optional[bytes],
) -> bytes:
    """Короткие имена моделей: dall-e-3 и т.п. через /v2/images/generations|edits."""
    if init_bytes:
        files = {"image": ("in.png", init_bytes, "image/png")}
        data_form = {
            "model": model,
            "prompt": prompt,
            "n": "1",
            "size": "1024x1024",
            "quality": "high",
            "response_format": "url",
        }
        r = await client.post(POLZA_IMAGE_EDITS_URL, headers=headers, files=files, data=data_form, timeout=API_IMAGE_TIMEOUT)
        if r.status_code == 404:
            r = await client.post(f"{base}/images/edits", headers=headers, files=files, data=data_form, timeout=API_IMAGE_TIMEOUT)
    else:
        body = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "quality": "high",
            "response_format": "url",
        }
        r = await client.post(POLZA_IMAGE_GENERATIONS_URL, headers=headers, json=body, timeout=API_IMAGE_TIMEOUT)
        if r.status_code == 404:
            r = await client.post(
                "https://polza.ai/api/v2/images/generations",
                headers=headers,
                json=body,
                timeout=API_IMAGE_TIMEOUT,
            )

    img = await _polza_extract_image_bytes(client, r)
    if img:
        return img
    if r.status_code in (200, 201):
        try:
            js_post = r.json()
        except Exception:
            js_post = {}
        rid = _polza_async_job_id(js_post)
        if rid:
            return await _polza_poll_media_until_image(client, headers, base, rid, API_IMAGE_TIMEOUT)
    raise RuntimeError(f"Polza v2 images: {r.status_code} {r.text[:500]}")


async def _polza_bytes_from_media_status(client: httpx.AsyncClient, js: dict) -> Optional[bytes]:
    """Готовый объект статуса completed или синхронный ответ с data."""
    data = js.get("data")
    if isinstance(data, dict):
        if data.get("url"):
            ir = await client.get(data["url"], timeout=90.0)
            ir.raise_for_status()
            return ir.content
        if data.get("b64_json"):
            return base64.b64decode(data["b64_json"])
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, dict):
            if row.get("url"):
                ir = await client.get(row["url"], timeout=90.0)
                ir.raise_for_status()
                return ir.content
            if row.get("b64_json"):
                return base64.b64decode(row["b64_json"])
    return None


async def _polza_poll_media_until_image(
    client: httpx.AsyncClient,
    headers: dict,
    base: str,
    media_id: str,
    max_wait: float,
) -> bytes:
    """Опрос GET /v1/media/{id} до completed (док. Polza.ai)."""
    bases = [base.rstrip("/"), POLZA_MEDIA_POLL_FALLBACK.rstrip("/")]
    t0 = time.time()
    last_log = ""
    while time.time() - t0 < max_wait:
        for b in bases:
            url = f"{b}/media/{media_id}"
            try:
                pr = await client.get(url, headers=headers, timeout=45.0)
                if pr.status_code == 404:
                    continue
                pr.raise_for_status()
                js = pr.json()
                st = (js.get("status") or "").lower()
                last_log = f"{url} status={st}"
                if st == "completed":
                    img = await _polza_bytes_from_media_status(client, js)
                    if img:
                        return img
                    raise RuntimeError(f"Polza: статус completed, но нет изображения в data: {repr(js)[:500]}")
                if st == "failed":
                    err = js.get("error") or {}
                    raise RuntimeError(f"Polza: генерация не удалась: {err}")
                if st in ("cancelled", "canceled"):
                    raise RuntimeError("Polza: генерация отменена.")
                break
            except httpx.HTTPStatusError as e:
                logger.info("Polza poll HTTP error %s: %s", url, e)
            except RuntimeError:
                raise
            except Exception as e:
                logger.info("Polza poll %s: %s", url, e)
        await asyncio.sleep(POLZA_POLL_INTERVAL_SEC)

    raise TimeoutError(f"Polza: таймаут {max_wait:.0f} с при опросе задачи {media_id}. {last_log}")


async def _polza_image(
    client: httpx.AsyncClient,
    api_key: str,
    prompt: str,
    init_bytes: Optional[bytes] = None,
    image_model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
) -> bytes:
    api_key = normalize_stored_api_key(api_key)
    headers = {"Authorization": f"Bearer {api_key}"}
    base = POLZA_IMAGE_BASE.rstrip("/")
    raw = (image_model or "").strip()
    model = raw or POLZA_MEDIA_MODEL_DEFAULT

    # Полный slug (openai/gpt-5.4-image-2, …) — только Polza Media API, без подмены на DALL·E
    if "/" in model:
        return await _polza_media_create_and_wait(
            client,
            headers,
            base,
            model,
            prompt,
            init_bytes,
            API_IMAGE_TIMEOUT,
            aspect_ratio=aspect_ratio,
        )

    # Короткие имена (dall-e-3, …) — OpenAI-совместимый /v2/images/*
    try:
        return await _polza_openai_v2_image(client, headers, base, model, prompt, init_bytes)
    except Exception as e:
        raise RuntimeError(
            f"Polza v2, модель «{model}»: {e}. "
            f"Для ChatGPT Image 2 задайте POLZA_MEDIA_MODEL_DEFAULT = «openai/gpt-5.4-image-2» "
            f"или полный slug модели в настройках (polza_model)."
        ) from e


Job = dict[str, Any]


def _user_friendly_gen_error(mode: Optional[str], exc: BaseException) -> str:
    """Короткое сообщение в чат; полный traceback остаётся в логе."""
    if isinstance(exc, PolzaUnauthorized):
        return (
            "❌ Polza отклонила API ключ (ошибка 401).\n"
            "Создайте ключ в консоли polza.ai и пришлите его снова одной строкой, без кавычек и пробелов по краям."
        )
    text = str(exc)
    low = text.lower()
    if mode == GEN_MODE_POLZA:
        if "401" in text or "unauthorized" in low or "некорректный api ключ" in low:
            return (
                "❌ Polza вернула 401: неверный или устаревший API ключ.\n"
                "Создайте ключ в личном кабинете polza.ai и пришлите его снова в настройках "
                "(кнопка «3 Polza…», затем «Ключ API»)."
            )
    if mode == GEN_MODE_OPENROUTER:
        if "401" in text or "unauthorized" in low or ("invalid" in low and "key" in low):
            return (
                "❌ OpenRouter отклонил ключ — проверьте API key на openrouter.ai "
                "и обновите в настройках (кнопка «2 OpenRouter…», затем «Ключ API»)."
            )
    return f"❌ Ошибка генерации:\n{text}"


async def _run_single_job(
    bot,
    http_client: httpx.AsyncClient,
    chat_id: int,
    user_data: dict,
    job: Job,
    queue_remaining_after: int,
) -> None:
    gs = get_gen_settings(user_data)
    mode = gs.get("mode")
    prompt = (job.get("prompt") or "").strip()
    kind = job.get("kind")  # txt2img | img2img
    file_id = job.get("image_file_id")

    if not prompt:
        await bot.send_message(chat_id, "❌ Пустой промт — задача пропущена.")
        return

    await bot.send_message(
        chat_id,
        f"⚙️ Запуск задачи из очереди. Режим: {mode}, тип: {kind}. "
        f"После выполнения останется в очереди: {queue_remaining_after}.",
    )
    t0 = time.time()
    img_bytes: Optional[bytes] = None

    try:
        init_bytes = None
        if kind == "img2img" and file_id:
            tg_file = await bot.get_file(file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(out=buf)
            init_bytes = buf.getvalue()

        if mode == GEN_MODE_SDXL:
            base = normalize_gradio_base(gs.get("gradio_url", ""))
            if kind == "img2img":
                if not init_bytes:
                    raise RuntimeError("Для img2img нужно загрузить изображение в бота.")
                denoise = float(gs.get("denoise", 0.65))
                img_bytes = await _sdxl_img2img_http(http_client, base, prompt, init_bytes, denoise)
            else:
                img_bytes = await _sdxl_txt2img_http(http_client, base, prompt)

        elif mode == GEN_MODE_OPENROUTER:
            key = gs.get("api_key")
            if not key:
                raise RuntimeError("Нет API ключа OpenRouter.")
            img_bytes = await _openrouter_image(http_client, key, prompt, init_bytes if kind == "img2img" else None)

        elif mode == GEN_MODE_POLZA:
            key = gs.get("api_key")
            if not key:
                raise RuntimeError("Нет API ключа Polza.")
            img_bytes = await _polza_image(
                http_client,
                key,
                prompt,
                init_bytes if kind == "img2img" else None,
                image_model=gs.get("polza_model"),
                aspect_ratio=gs.get("polza_aspect_ratio"),
            )

        else:
            raise RuntimeError("Режим генерации не выбран.")

        elapsed = time.time() - t0
        await bot.send_photo(
            chat_id,
            photo=img_bytes,
            caption=f"✅ Готово за {elapsed:.1f} с\nПромт: {prompt[:900]}",
        )
    except PolzaUnauthorized as e:
        elapsed = time.time() - t0
        logger.warning("generation job failed: Polza 401 — invalid API key (%s)", e)
        user_msg = _user_friendly_gen_error(mode, e)
        await bot.send_message(chat_id, f"{user_msg}\n\n(за {elapsed:.1f} с)")
    except Exception as e:
        elapsed = time.time() - t0
        logger.exception("generation job failed")
        user_msg = _user_friendly_gen_error(mode, e)
        await bot.send_message(chat_id, f"{user_msg}\n\n(за {elapsed:.1f} с)")


async def generation_worker_loop(bot, application, chat_id: int, user_data: dict) -> None:
    """Последовательно выполняет задачи из очереди (один воркер на чат)."""
    http_client = application.bot_data.get("http_client")
    if not http_client:
        async with _lock(chat_id):
            user_data["_gen_worker_running"] = False
        return

    lock = _lock(chat_id)
    try:
        while True:
            async with lock:
                q = user_data.get("gen_queue")
                if not q or len(q) == 0:
                    user_data["_gen_worker_running"] = False
                    return
                job = q.popleft()
                remaining = len(q)

            await _run_single_job(bot, http_client, chat_id, user_data, job, remaining)

            if remaining:
                await bot.send_message(
                    chat_id,
                    f"📋 В очереди ещё {remaining} задач (макс. {MAX_QUEUE_LEN}).",
                )
    except Exception as e:
        logger.exception("generation_worker_loop: %s", e)
        async with lock:
            user_data["_gen_worker_running"] = False


async def enqueue_generation(
    bot,
    application,
    chat_id: int,
    user_data: dict,
    kind: str,
    prompt: str,
    image_file_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Возвращает (ok, status):
    - started — запущен воркер с этой задачей
    - queued — добавлено, воркер уже крутится
    - full — очередь полна
    """
    lock = _lock(chat_id)
    async with lock:
        q = user_data.get("gen_queue")
        if isinstance(q, list):
            q = deque(q)
            user_data["gen_queue"] = q
        elif not isinstance(q, deque):
            q = deque()
            user_data["gen_queue"] = q

        if len(q) >= MAX_QUEUE_LEN:
            return False, "full"

        job: Job = {"kind": kind, "prompt": prompt, "image_file_id": image_file_id}
        q.append(job)

        was_idle = not user_data.get("_gen_worker_running")
        if was_idle:
            user_data["_gen_worker_running"] = True

    if was_idle:
        asyncio.create_task(generation_worker_loop(bot, application, chat_id, user_data))
        return True, "started"
    return True, "queued"


def validate_url(text: str) -> Optional[str]:
    t = text.strip()
    if not re.match(r"^https?://", t):
        return None
    return normalize_gradio_base(t)


def validate_denoise(text: str) -> Optional[float]:
    try:
        x = float(text.replace(",", "."))
        if 0.0 <= x <= 1.0:
            return x
    except ValueError:
        pass
    return None
