"""User-supplied render assets: BGM tracks and watermark logos (Phase 4).

Files live on disk (`data/assets/bgm`, `data/assets/watermarks`) so they survive
restarts and can be reused across projects; no database is involved. Every name
is normalised to a slug and re-validated on the way out, so a project preset can
only ever reference a file inside those two folders.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from backend import config

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

_KINDS: dict[str, tuple[Path, set[str]]] = {
    "bgm": (config.BGM_DIR, AUDIO_EXTS),
    "watermarks": (config.WATERMARK_DIR, IMAGE_EXTS),
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


class AssetInvalid(RuntimeError):
    """Bad asset request (unknown kind, wrong extension, too large, …)."""


def _folder(kind: str) -> tuple[Path, set[str]]:
    entry = _KINDS.get(kind)
    if entry is None:
        raise AssetInvalid(f"Unknown asset kind '{kind}'.")
    return entry


def _slug(value: str, fallback: str = "asset") -> str:
    cleaned = _SLUG_RE.sub("-", (value or "").lower()).strip("-._")
    return cleaned[:60].strip("-._") or fallback


def _url(kind: str, name: str) -> str:
    return f"/api/assets/{kind}/{name}"


def list_assets() -> dict[str, Any]:
    """Everything currently available, newest first."""
    payload: dict[str, Any] = {}
    for kind, (folder, _exts) in _KINDS.items():
        items: list[dict[str, Any]] = []
        if folder.is_dir():
            for path in sorted(folder.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                items.append(
                    {
                        "kind": kind,
                        "name": path.name,
                        "size_bytes": stat.st_size,
                        "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                        "url": _url(kind, path.name),
                    }
                )
        payload[kind] = items
    payload["limits"] = {
        "max_mb": config.ASSET_MAX_MB,
        "bgm_extensions": sorted(AUDIO_EXTS),
        "watermark_extensions": sorted(IMAGE_EXTS),
    }
    return payload


def save(kind: str, original_name: str, data: bytes) -> dict[str, Any]:
    folder, exts = _folder(kind)
    suffix = Path(original_name or "").suffix.lower()
    if suffix not in exts:
        raise AssetInvalid(
            f"Unsupported file type '{suffix or 'unknown'}'. Allowed: {', '.join(sorted(exts))}."
        )
    if not data:
        raise AssetInvalid("The uploaded file is empty.")
    limit = int(config.ASSET_MAX_MB * 1024 * 1024)
    if len(data) > limit:
        raise AssetInvalid(f"File is larger than the {config.ASSET_MAX_MB:g} MB limit.")

    folder.mkdir(parents=True, exist_ok=True)
    stem = _slug(Path(original_name).stem, fallback=kind)
    target = folder / f"{stem}{suffix}"
    counter = 2
    while target.exists():
        target = folder / f"{stem}-{counter}{suffix}"
        counter += 1

    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    stat = target.stat()
    return {
        "kind": kind,
        "name": target.name,
        "size_bytes": stat.st_size,
        "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
        "url": _url(kind, target.name),
    }


def resolve(kind: str, name: str) -> Path:
    """Absolute path for an asset, refusing anything outside its folder."""
    folder, _exts = _folder(kind)
    cleaned = (name or "").strip()
    if not _NAME_RE.fullmatch(cleaned):
        raise AssetInvalid("Invalid asset name.")
    path = (folder / cleaned).resolve()
    root = folder.resolve()
    if path.parent != root or not path.is_file():
        raise AssetInvalid("Asset not found.")
    return path


def delete(kind: str, name: str) -> None:
    path = resolve(kind, name)
    path.unlink(missing_ok=True)
