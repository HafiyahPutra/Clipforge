"""Media intake: yt-dlp downloads (YouTube / Google Drive / direct URLs) + ffmpeg helpers.

Everything lands in `temp/jobs/<job_id>/` and is removed by the temp cleanup
worker later, so a long analysis never leaves gigabytes behind.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from backend import config

ProgressFn = Callable[[float, str], None]


class DownloadError(RuntimeError):
    """Raised when a URL cannot be resolved or downloaded."""


class MediaError(RuntimeError):
    """Raised when ffmpeg/ffprobe cannot process a file."""


class _Cancelled(Exception):
    """Internal signal used to abort a yt-dlp download."""


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(
            f"`{name}` was not found on PATH. On macOS install it with `brew install ffmpeg` "
            "(ffmpeg ships ffprobe too)."
        )
    return path


def probe(path: str | Path) -> dict[str, Any]:
    """Return duration/size info for a media file via ffprobe."""
    binary = require_binary(config.FFPROBE_BIN)
    cmd = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"ffprobe failed: {exc}") from exc
    if completed.returncode != 0:
        raise MediaError(f"ffprobe could not read the file: {completed.stderr.strip()[:300]}")

    payload = json.loads(completed.stdout or "{}")
    fmt = payload.get("format", {})
    video_stream = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    audio_stream = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    duration = float(fmt.get("duration") or video_stream.get("duration") or 0.0)
    return {
        "duration": round(duration, 3),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "has_video": bool(video_stream),
        "has_audio": audio_stream is not None,
        "size_bytes": int(fmt.get("size") or 0),
        "title": (fmt.get("tags") or {}).get("title") or Path(path).stem,
    }


def extract_audio(video_path: str | Path, wav_path: str | Path) -> Path:
    """Extract a 16 kHz mono WAV — the format Whisper and the RMS heatmap want."""
    binary = require_binary(config.FFMPEG_BIN)
    target = Path(wav_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary,
        "-y",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"ffmpeg could not extract audio: {exc}") from exc
    if completed.returncode != 0 or not target.exists():
        raise MediaError(
            "ffmpeg could not extract audio: "
            f"{completed.stderr.strip()[:300] or 'unknown error'}"
        )
    return target


def download(
    url: str,
    dest_dir: str | Path,
    *,
    cookies_file: str | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Download `url` into `dest_dir` and return the resolved metadata.

    Supports YouTube, Google Drive links and any direct media URL that yt-dlp's
    generic extractor understands.
    """
    if not url.strip():
        raise DownloadError("No URL provided.")

    if _source_type(url) == "gdrive":
        return _download_from_drive(url, Path(dest_dir), on_progress=on_progress, cancel=cancel)

    try:
        import yt_dlp  # imported lazily so the API boots without the media stack
    except ImportError as exc:
        raise DownloadError(
            "yt-dlp is not installed. Run `pip install -r backend/requirements.txt`."
        ) from exc

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    def hook(payload: dict[str, Any]) -> None:
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        if on_progress is None:
            return
        if payload.get("status") == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            done = payload.get("downloaded_bytes") or 0
            if total:
                on_progress(min(99.0, done / total * 100.0), "Downloading video")
            else:
                on_progress(50.0, "Downloading video")
        elif payload.get("status") == "finished":
            on_progress(99.0, "Download finished, merging streams")

    max_height = config.YTDLP_MAX_HEIGHT
    ydl_opts: dict[str, Any] = {
        "outtmpl": str(dest / "source.%(ext)s"),
        "format": (
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_height}]/bv*[height<={max_height}]+ba/b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": config.YTDLP_SOCKET_TIMEOUT,
        "retries": 3,
        "progress_hooks": [hook],
        "restrictfilenames": True,
    }
    cookie_path = cookies_file or config.YTDLP_COOKIES_FILE or None
    if cookie_path:
        ydl_opts["cookiefile"] = cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except _Cancelled as exc:
        raise DownloadError("Download cancelled") from exc
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
        raise DownloadError(_friendly_ytdlp_error(exc)) from exc

    if info is None:
        raise DownloadError("yt-dlp returned no metadata for this URL.")
    if "entries" in info:  # a playlist link: take the first entry
        entries = [entry for entry in info["entries"] if entry]
        if not entries:
            raise DownloadError("This playlist contains no downloadable video.")
        info = entries[0]

    requested = (info.get("requested_downloads") or [{}])[0]
    file_path = requested.get("filepath") or info.get("_filename")
    if not file_path or not Path(file_path).exists():
        candidates = sorted(
            (item for item in dest.glob("source.*")),
            key=lambda item: item.stat().st_size,
            reverse=True,
        )
        if not candidates:
            raise DownloadError("The download finished but no media file was found.")
        file_path = str(candidates[0])

    if on_progress:
        on_progress(100.0, "Download complete")

    thumbnails = info.get("thumbnails") or []
    thumbnail = info.get("thumbnail") or (
        thumbnails[-1].get("url") if thumbnails and isinstance(thumbnails[-1], dict) else None
    )
    if not thumbnail and _source_type(url) == "youtube" and info.get("id"):
        thumbnail = f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"

    return {
        "local_path": str(file_path),
        "video_id": info.get("id"),
        "title": info.get("title") or Path(file_path).stem,
        "channel": info.get("channel") or info.get("uploader") or info.get("creator"),
        "duration": float(info.get("duration") or 0.0),
        "thumbnail": thumbnail,
        "webpage_url": info.get("webpage_url") or url,
        "source_type": _source_type(url),
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
    }


_DRIVE_ID = re.compile(r"(?:/file/d/|/document/d/|/d/|[?&]id=)([A-Za-z0-9_-]{10,})")
_DRIVE_MIME_EXT = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}


def _drive_filename(disposition: str) -> str:
    """Extract the original filename from a Content-Disposition header."""
    if not disposition:
        return ""
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
    if encoded:
        from urllib.parse import unquote

        return unquote(encoded.group(1)).strip().strip('"')
    plain = re.search(r'filename="?([^";]+)"?', disposition)
    return plain.group(1).strip() if plain else ""


def _drive_file_id(url: str) -> str:
    match = _DRIVE_ID.search(url)
    if not match:
        raise DownloadError(
            "That Google Drive link has no file id. Use the share link of a single file, "
            "e.g. https://drive.google.com/file/d/<id>/view — folders are not supported."
        )
    return match.group(1)


def _download_from_drive(
    url: str,
    dest: Path,
    *,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Download a public Google Drive file through the `uc?export=download` endpoint.

    Drive answers large files with an HTML confirmation page (or a
    `download_warning` cookie), so the request is repeated once with the token.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise DownloadError("`requests` is not installed. Reinstall the backend dependencies.") from exc

    dest.mkdir(parents=True, exist_ok=True)
    file_id = _drive_file_id(url)
    # Drive moved large-file downloads to drive.usercontent.google.com; the old
    # `uc?export=download` endpoint answers with a virus-scan confirmation page.
    endpoint = "https://drive.usercontent.google.com/download"
    timeout = max(30, config.YTDLP_SOCKET_TIMEOUT)
    params = {"export": "download", "id": file_id, "confirm": "t"}

    with requests.Session() as session:
        response = session.get(endpoint, params=params, stream=True, timeout=timeout)
        if response.status_code >= 400:
            raise DownloadError(
                f"Google Drive returned HTTP {response.status_code} for this file. "
                "Check that the link is public."
            )

        confirm: str | None = None
        for key, value in response.cookies.items():
            if key.startswith("download_warning"):
                confirm = value
        content_type = (response.headers.get("content-type") or "").lower()
        if confirm is None and "text/html" in content_type:
            page = response.text
            found = re.search(r'name="confirm"\s+value="([^"]+)"', page) or re.search(
                r"confirm=([0-9A-Za-z_-]+)", page
            )
            response.close()
            if not found:
                raise DownloadError(
                    "Google Drive served a web page instead of a video. Share the file with "
                    "'Anyone with the link' and make sure it is a video, not a folder or a Google Doc."
                )
            confirm = found.group(1)
            response = session.get(endpoint, params={**params, "confirm": confirm}, stream=True, timeout=timeout)
            if response.status_code >= 400:
                raise DownloadError(f"Google Drive refused the download (HTTP {response.status_code}).")
            content_type = (response.headers.get("content-type") or "").lower()

        if "text/html" in content_type:
            response.close()
            raise DownloadError(
                "Google Drive keeps returning a web page instead of the video. The file may be "
                "private, rate-limited, or still being scanned for viruses — retry in a minute."
            )

        extension = _DRIVE_MIME_EXT.get(content_type.split(";")[0].strip(), ".mp4")
        drive_name = _drive_filename(response.headers.get("content-disposition") or "")
        target = dest / f"source{extension}"
        total = int(response.headers.get("content-length") or 0)
        written = 0
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("Download cancelled")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if on_progress and total:
                        on_progress(min(99.0, written / total * 100.0), "Downloading from Google Drive")
        finally:
            response.close()

    if written == 0:
        target.unlink(missing_ok=True)
        raise DownloadError("Google Drive returned an empty file.")

    if on_progress:
        on_progress(100.0, "Download complete")

    try:
        info = probe(target)
        duration = float(info.get("duration") or 0.0)
    except MediaError:
        duration = 0.0

    return {
        "local_path": str(target),
        "video_id": file_id,
        "title": Path(drive_name).stem if drive_name else f"Google Drive {file_id[:6]}",
        "channel": None,
        "duration": duration,
        "thumbnail": f"https://drive.google.com/thumbnail?id={file_id}&sz=w640",
        "webpage_url": url,
        "source_type": "gdrive",
        "extractor": "google-drive",
    }


def _source_type(url: str) -> str:
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if "drive.google.com" in lowered or "docs.google.com" in lowered:
        return "gdrive"
    return "url"


def _friendly_ytdlp_error(exc: Exception) -> str:
    detail = str(exc).strip() or type(exc).__name__
    lowered = detail.lower()
    if "sign in" in lowered or "confirm your age" in lowered or "private" in lowered:
        return (
            f"The video is not publicly accessible ({detail}). "
            "Provide a cookies file in Settings or use a public video."
        )
    if "unsupported url" in lowered:
        return f"This URL is not supported by yt-dlp ({detail})."
    if "unable to download" in lowered or "http error 403" in lowered:
        return f"The site refused the download ({detail}). Try again or use a cookies file."
    if "timed out" in lowered or "timeout" in lowered:
        return "The download timed out. Check your connection and retry."
    return f"Download failed: {detail[:300]}"
