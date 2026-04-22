import asyncio
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from rubpy import Client as RubikaClient

from task_store import (
    append_failed,
    build_status_text,
    clear_cancelled,
    clear_processing,
    cleanup_local_file,
    ensure_storage_dirs,
    is_cancelled,
    load_processing,
    pop_first_task,
    save_processing,
)


load_dotenv()

SESSION = os.getenv("RUBIKA_SESSION", "rubika_session").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MAX_RETRIES = 5
RETRY_DELAY = 3
TARGET = "me"

ensure_storage_dirs()


COMMON_RUBIKA_SAFE_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".pdf", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
}


class CancelledTaskError(RuntimeError):
    pass


class FilenameFallbackRequired(RuntimeError):
    pass


def remove_extension(name: str) -> str:
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name or "file"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem or "file"
    suffix = path.suffix
    index = 1

    while True:
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def has_session(session_name: str) -> bool:
    candidates = [
        Path(session_name),
        Path(f"{session_name}.session"),
        Path(f"{session_name}.sqlite"),
    ]
    return any(path.exists() for path in candidates)


def ensure_session():
    if has_session(SESSION):
        return

    async def bootstrap():
        async with RubikaClient(name=SESSION):
            return None

    asyncio.run(bootstrap())
    print("Login successful.")


def should_keep_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in COMMON_RUBIKA_SAFE_EXTENSIONS


def update_telegram_status(
    task: dict,
    stage: str,
    upload_status: str,
    note: str | None = None,
    attempt_text: str | None = None,
) -> None:
    if not BOT_TOKEN:
        return

    chat_id = task.get("chat_id")
    status_message_id = task.get("status_message_id")
    if not chat_id or not status_message_id:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": status_message_id,
                "text": build_status_text(
                    task_id=task.get("task_id", "-"),
                    file_name=task.get("file_name", Path(task.get("path", "")).name or "file"),
                    file_size=int(task.get("file_size", 0) or 0),
                    stage=stage,
                    download_percent=100,
                    upload_percent=int(task.get("upload_percent", 0) or 0),
                    upload_status=upload_status,
                    note=note,
                    attempt_text=attempt_text or task.get("attempt_text"),
                ),
                "parse_mode": "HTML",
            },
            timeout=15,
        )
    except Exception:
        pass


async def send_document(
    file_path: str,
    caption: str = "",
    callback=None,
    file_name: str | None = None,
):
    async with RubikaClient(name=SESSION) as client:
        return await client.send_document(
            TARGET,
            file_path,
            caption=caption or "",
            callback=callback,
            file_name=file_name or Path(file_path).name,
        )


def is_transient_upload_error(error_text: str) -> bool:
    return any(
        key in error_text
        for key in [
            "502",
            "bad gateway",
            "timeout",
            "cannot connect",
            "connection reset",
            "temporarily unavailable",
            "error uploading chunk",
        ]
    )


def should_use_extensionless_fallback(
    *,
    media_type: str,
    file_name: str,
    error_text: str,
) -> bool:
    has_extension = Path(file_name).suffix != ""
    if not has_extension:
        return False

    lowered = error_text.lower()
    is_chunk_error = "error uploading chunk" in lowered
    if media_type == "document" and is_chunk_error:
        return True

    return not should_keep_extension(file_name)


def should_force_extensionless_before_upload(media_type: str, file_name: str) -> bool:
    suffix = Path(file_name).suffix.lower()
    return media_type == "document" and suffix in ARCHIVE_EXTENSIONS


def wait_with_cancel(task_id: str, seconds: int) -> None:
    for _ in range(seconds):
        if is_cancelled(task_id):
            raise CancelledTaskError("Cancelled by user.")
        time.sleep(1)


def normalize_failed_progress(task: dict) -> None:
    current_percent = int(task.get("upload_percent", 0) or 0)
    task["upload_percent"] = min(current_percent, 99)


def prepare_extensionless_upload_file(task: dict, source_path: Path, display_name: str) -> Path:
    fallback_name = remove_extension(display_name)
    target_path = unique_path(source_path.with_name(fallback_name))

    if source_path != target_path:
        source_path.rename(target_path)

    task["path"] = str(target_path)
    task["file_name"] = target_path.name
    task["upload_percent"] = 0
    task["attempt_text"] = None
    save_processing(task)
    return target_path


def make_upload_progress_callback(task: dict, attempt: int):
    state = {"last_percent": -1, "last_update": 0.0}
    task_id = task.get("task_id", "")

    async def callback(total: int, current: int) -> None:
        if total <= 0:
            return

        percent = min(100, max(0, int((current * 100) / total)))
        now = time.monotonic()
        should_emit = (
            percent == 100
            or state["last_percent"] < 0
            or percent - state["last_percent"] >= 5
            or now - state["last_update"] >= 2
        )

        if not should_emit:
            return

        state["last_percent"] = percent
        state["last_update"] = now
        task["upload_percent"] = percent
        task["attempt_text"] = f"{attempt} از {MAX_RETRIES}"
        save_processing(task)
        update_telegram_status(
            task,
            stage="در حال آپلود به روبیکا",
            upload_status="در حال ارسال فایل",
            note="پیشرفت آپلود به صورت لحظه‌ای به‌روزرسانی می‌شود.",
            attempt_text=task["attempt_text"],
        )

        if is_cancelled(task_id):
            raise CancelledTaskError("Cancelled by user.")

    return callback


def send_with_retry(
    task: dict,
    file_path: str,
    caption: str = "",
    file_name: str | None = None,
):
    task_id = task.get("task_id", "")
    media_type = task.get("media_type", "")
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        if is_cancelled(task_id):
            raise CancelledTaskError("Cancelled by user.")

        task["upload_percent"] = 0
        task["attempt_text"] = f"{attempt} از {MAX_RETRIES}"
        save_processing(task)
        update_telegram_status(
            task,
            stage="در حال آپلود به روبیکا",
            upload_status="در حال شروع آپلود",
            note="اگر خطای موقت رخ بدهد، worker خودش دوباره تلاش می‌کند.",
            attempt_text=task["attempt_text"],
        )

        try:
            return asyncio.run(
                send_document(
                    file_path,
                    caption,
                    callback=make_upload_progress_callback(task, attempt),
                    file_name=file_name,
                )
            )
        except Exception as e:
            last_error = e
            error_text = str(e).lower()
            effective_file_name = file_name or Path(file_path).name
            task["attempt_text"] = f"{attempt} از {MAX_RETRIES}"
            normalize_failed_progress(task)
            save_processing(task)

            if should_use_extensionless_fallback(
                media_type=media_type,
                file_name=effective_file_name,
                error_text=error_text,
            ):
                raise FilenameFallbackRequired(str(e)) from e

            transient = is_transient_upload_error(error_text)

            if transient and attempt < MAX_RETRIES:
                delay = RETRY_DELAY * attempt
                next_attempt_text = f"{attempt + 1} از {MAX_RETRIES}"
                task["upload_percent"] = 0
                task["attempt_text"] = next_attempt_text
                save_processing(task)
                update_telegram_status(
                    task,
                    stage="خطای موقت در آپلود",
                    upload_status="آپلود موقتا قطع شد",
                    note=f"تلاش {attempt} ناموفق بود. {delay} ثانیه دیگر تلاش بعدی شروع می‌شود.",
                    attempt_text=next_attempt_text,
                )
                wait_with_cancel(task_id, delay)
                continue

            break

    raise last_error if last_error else RuntimeError("Upload failed.")


def process_task(task: dict) -> None:
    task_type = task.get("type")
    if task_type != "local_file":
        raise RuntimeError("Unknown task type.")

    task_id = task.get("task_id", "")
    caption = task.get("caption", "")
    media_type = task.get("media_type", "")
    original_path = Path(task.get("path", ""))
    if not original_path.exists():
        raise RuntimeError("Local file not found.")

    send_path = original_path
    send_name = task.get("file_name") or original_path.name

    try:
        if is_cancelled(task_id):
            raise CancelledTaskError("Cancelled before upload started.")

        update_telegram_status(
            task,
            stage="نوبت شما رسید",
            upload_status="آماده‌سازی فایل برای روبیکا",
            note="آپلود تا چند لحظه دیگر شروع می‌شود.",
        )

        if should_force_extensionless_before_upload(media_type, send_name):
            send_path = prepare_extensionless_upload_file(task, send_path, send_name)
            send_name = Path(task["path"]).name
            update_telegram_status(
                task,
                stage="آماده‌سازی فایل برای روبیکا",
                upload_status="نام فایل برای روبیکا تنظیم شد",
                note="فایل فشرده برای سازگاری بیشتر بدون پسوند ارسال می‌شود.",
            )

        task["file_name"] = send_name
        save_processing(task)

        try:
            send_with_retry(task, str(send_path), caption, file_name=send_name)
        except CancelledTaskError:
            raise
        except FilenameFallbackRequired as e:
            fallback_name = remove_extension(send_name)
            if fallback_name == send_name:
                raise RuntimeError(str(e)) from e

            send_path = prepare_extensionless_upload_file(task, send_path, fallback_name)
            update_telegram_status(
                task,
                stage="تلاش دوباره با نام سازگار",
                upload_status="نام فایل برای روبیکا تنظیم شد",
                note="ارسال با نام اصلی خطا داد و حالا بدون پسوند دوباره تلاش می‌شود.",
            )
            send_name = Path(task["path"]).name
            send_with_retry(task, str(send_path), caption, file_name=send_name)
        except Exception:
            fallback_name = remove_extension(send_name)
            needs_fallback = (
                fallback_name != send_name
                and (
                    media_type == "document"
                    or not should_keep_extension(send_name)
                )
            )
            if not needs_fallback:
                raise

            send_path = prepare_extensionless_upload_file(task, send_path, fallback_name)
            update_telegram_status(
                task,
                stage="تلاش دوباره با نام سازگار",
                upload_status="نام فایل برای روبیکا تنظیم شد",
                note="این فایل ابتدا با نام اصلی ارسال شد و حالا بدون پسوند دوباره تلاش می‌شود.",
            )
            send_name = Path(task["path"]).name
            send_with_retry(task, str(send_path), caption, file_name=send_name)
    except CancelledTaskError:
        cleanup_local_file(str(send_path))
        clear_cancelled(task_id)
        update_telegram_status(
            task,
            stage="لغو شد",
            upload_status="ارسال متوقف شد",
            note="فایل موقت پاک شد.",
            attempt_text=task.get("attempt_text"),
        )
        return
    except Exception:
        clear_cancelled(task_id)
        raise

    cleanup_local_file(str(send_path))
    clear_cancelled(task_id)
    task["upload_percent"] = 100
    save_processing(task)
    update_telegram_status(
        task,
        stage="ارسال شد",
        upload_status="موفق",
        note="فایل موقت پاک شد و کار کامل شد.",
        attempt_text=task.get("attempt_text"),
    )


def worker_loop():
    ensure_session()
    print("Rubika worker started.")

    while True:
        task = pop_first_task()

        if not task:
            time.sleep(0.2)
            continue

        save_processing(task)

        try:
            process_task(task)
        except CancelledTaskError:
            processing_task = load_processing() or task
            clear_cancelled(processing_task.get("task_id", ""))
            update_telegram_status(
                processing_task,
                stage="لغو شد",
                upload_status="ارسال متوقف شد",
                note="فایل موقت پاک شد.",
                attempt_text=processing_task.get("attempt_text"),
            )
        except Exception as e:
            processing_task = load_processing() or task
            processing_task["attempt_text"] = processing_task.get("attempt_text") or f"{MAX_RETRIES} از {MAX_RETRIES}"
            normalize_failed_progress(processing_task)
            save_processing(processing_task)
            append_failed(processing_task, str(e))
            update_telegram_status(
                processing_task,
                stage="خطا در آپلود",
                upload_status=f"پس از {MAX_RETRIES} تلاش ناموفق شد",
                note="فایل موقت برای بررسی و تلاش دوباره نگه داشته شد.",
                attempt_text=processing_task.get("attempt_text"),
            )
        finally:
            clear_processing()


if __name__ == "__main__":
    worker_loop()
