"""File store: Anthropic Files API uploads plus client-local attachment mirroring.

Blobs live under ``<data>/files/<user_id>/`` and are addressed by server-generated
ids, so a caller can never influence the on-disk path.  Filenames are only kept as
metadata and are sanitised before storage.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, UploadFile
from starlette.datastructures import UploadFile as StarletteUploadFile

from .config import Settings
from .db import Database, now_ms, utc_iso
from .auth import authenticate
from .models import ApiError, FileDeleted, FileObject, not_found
from .security import check_storage_quota, reject_oversized_content_length

router = APIRouter(tags=["files"])

_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fff ]+", re.UNICODE)
_SAFE_LOCAL_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_CHUNK = 64 * 1024


def sanitize_filename(name: str | None, fallback: str = "file") -> str:
    """Reduce a client-supplied filename to a safe display name.

    Spaces and CJK characters are preserved; path separators, control characters
    and anything exotic become ``_`` so a filename can never influence the
    on-disk path (which is the server-generated id anyway).
    """
    raw = (name or "").replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    cleaned = _SAFE_NAME.sub("_", raw).strip(" ._")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:180]


def safe_extension(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
        return ""
    return suffix


#: Conservative allow-list for client-declared MIME types.  Anything else is
#: stored as application/octet-stream — we never echo an attacker-chosen type.
ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/zip",
        "application/octet-stream",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/xml",
        "text/css",
        "text/javascript",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/svg+xml",
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/opus",
        "audio/webm",
        "audio/flac",
        "audio/aac",
        "audio/pcm",
        "video/mp4",
        "video/webm",
        "video/ogg",
        "video/quicktime",
    }
)

#: Types that a browser may execute as script when rendered inline.
SCRIPTABLE_MIME_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "application/ecmascript",
    }
)


def is_scriptable_mime(mime_type: str | None) -> bool:
    mime = (mime_type or "").lower().split(";")[0].strip()
    return (
        mime in SCRIPTABLE_MIME_TYPES
        or mime.endswith("+xml")
        or "html" in mime
    )


def sanitize_mime(filename: str, declared: str | None) -> str:
    """Pick a MIME type from the allow-list; never trust the client blindly."""
    candidate = (declared or "").split(";")[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename or "")
    for value in (candidate, (guessed or "").lower()):
        if value and value in ALLOWED_MIME_TYPES:
            return value
    return "application/octet-stream"


def guess_mime(filename: str, declared: str | None) -> str:
    """Backwards-compatible alias for :func:`sanitize_mime`."""
    return sanitize_mime(filename, declared)


def file_response_headers(mime_type: str, filename: str) -> dict[str, str]:
    """Defence in depth for user-supplied files.

    Script-capable types are always a download (never rendered inline) and are
    additionally sandboxed; every file response forbids MIME sniffing.
    """
    scriptable = is_scriptable_mime(mime_type)
    headers = {
        "Content-Disposition": content_disposition(
            filename, mime_type, force_attachment=scriptable
        ),
        "X-Content-Type-Options": "nosniff",
    }
    if scriptable:
        headers["Content-Security-Policy"] = "sandbox"
    return headers


@dataclass(slots=True)
class FileRecord:
    id: str
    user_id: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: int
    purpose: str | None = None
    sha256: str = ""

    def to_object(self) -> FileObject:
        return FileObject(
            id=self.id,
            size_bytes=self.size_bytes,
            created_at=utc_iso(self.created_at),
            filename=self.filename,
            mime_type=self.mime_type,
        )


class FileStore:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    # -- paths -------------------------------------------------------------
    def user_dir(self, user_id: str) -> Path:
        safe_user = re.sub(r"[^A-Za-z0-9_-]", "", user_id) or "anon"
        return self.settings.files_dir / safe_user

    def _path(self, user_id: str, name: str) -> Path:
        return self.user_dir(user_id) / name

    def _ensure_dir(self, user_id: str) -> Path:
        directory = self.user_dir(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _resolve_within(directory: Path, name: str) -> Path:
        target = (directory / name).resolve()
        base = directory.resolve()
        if base != target.parent:
            raise ApiError(400, "非法的文件路径", "invalid_request_error")
        return target

    # -- server file ids ---------------------------------------------------
    async def save_upload(
        self,
        user_id: str,
        upload: UploadFile,
        purpose: str | None,
        *,
        max_bytes: int | None = None,
    ) -> FileRecord:
        limit = max_bytes or self.settings.max_upload_bytes
        file_id = f"file_{secrets.token_hex(16)}"
        directory = self._ensure_dir(user_id)
        filename = sanitize_filename(upload.filename, fallback=f"{file_id}.bin")
        mime = guess_mime(filename, upload.content_type)
        temp_path = directory / f".{file_id}.part"
        target = self._resolve_within(directory, file_id)
        digest = hashlib.sha256()
        total = 0
        try:
            with temp_path.open("wb") as handle:
                while True:
                    chunk = await upload.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ApiError(
                            413,
                            f"文件过大：上限 {limit} 字节",
                            "request_too_large",
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            # Per-user storage quota (USER_STORAGE_MB, 0 = unlimited) is checked
            # once the size is known, before the blob is committed.
            await check_storage_quota(self.db, self.settings, user_id, total)
            os.replace(temp_path, target)
        except ApiError:
            temp_path.unlink(missing_ok=True)
            raise
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        record = FileRecord(
            id=file_id,
            user_id=user_id,
            filename=filename,
            mime_type=mime,
            size_bytes=total,
            created_at=now_ms(),
            purpose=purpose,
            sha256=digest.hexdigest(),
        )
        await self.db.execute(
            "INSERT INTO files (id, user_id, filename, mime_type, size_bytes, purpose, sha256,"
            " created_at, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                record.id,
                user_id,
                filename,
                mime,
                total,
                purpose,
                record.sha256,
                record.created_at,
            ),
        )
        return record

    async def metadata(self, user_id: str, file_id: str) -> FileRecord | None:
        row = await self.db.fetch_one(
            "SELECT * FROM files WHERE id = ? AND user_id = ? AND deleted = 0",
            (file_id, user_id),
        )
        if row is None:
            return None
        return FileRecord(
            id=row["id"],
            user_id=row["user_id"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            created_at=int(row["created_at"]),
            purpose=row["purpose"],
            sha256=row["sha256"],
        )

    async def read(self, user_id: str, file_id: str) -> tuple[bytes, FileRecord]:
        record = await self.metadata(user_id, file_id)
        if record is None:
            raise not_found(f"文件不存在：{file_id}")
        directory = self.user_dir(user_id)
        path = self._resolve_within(directory, file_id)
        if not path.is_file():
            raise not_found(f"文件已丢失：{file_id}")
        return path.read_bytes(), record

    async def read_limited(self, user_id: str, file_id: str, limit: int) -> tuple[bytes, FileRecord]:
        record = await self.metadata(user_id, file_id)
        if record is None:
            raise not_found(f"文件不存在：{file_id}")
        path = self._resolve_within(self.user_dir(user_id), file_id)
        if not path.is_file():
            raise not_found(f"文件已丢失：{file_id}")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ApiError(413, f"文件过大：上限 {limit} 字节", "request_too_large")
        return data, record

    async def delete(self, user_id: str, file_id: str) -> bool:
        record = await self.metadata(user_id, file_id)
        if record is None:
            return False
        await self.db.execute(
            "UPDATE files SET deleted = 1 WHERE id = ? AND user_id = ?", (file_id, user_id)
        )
        path = self._resolve_within(self.user_dir(user_id), file_id)
        path.unlink(missing_ok=True)
        return True

    async def delete_all_for_user(self, user_id: str) -> None:
        directory = self.user_dir(user_id)
        if directory.is_dir():
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
        await self.db.execute("DELETE FROM files WHERE user_id = ?", (user_id,))

    # -- client local attachments -----------------------------------------
    async def save_client_file(
        self, user_id: str, local_id: str, data: bytes, content_type: str | None
    ) -> dict[str, Any]:
        name = sanitize_local_id(local_id)
        directory = self._ensure_dir(user_id)
        path = self._resolve_within(directory, f"client_{name}")
        temp_path = directory / f".client_{name}.part"
        with temp_path.open("wb") as handle:
            handle.write(data)
        os.replace(temp_path, path)
        digest = hashlib.sha256(data).hexdigest()
        stamp = now_ms()
        mime = sanitize_mime(name, content_type)
        await self.db.execute(
            "INSERT INTO client_files (user_id, local_id, mime_type, size_bytes, sha256,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, local_id) DO UPDATE SET mime_type=excluded.mime_type,"
            " size_bytes=excluded.size_bytes, sha256=excluded.sha256,"
            " updated_at=excluded.updated_at",
            (user_id, name, mime, len(data), digest, stamp, stamp),
        )
        return {
            "id": name,
            "size": len(data),
            "contentType": mime,
            "sha256": digest,
            "updatedAt": stamp,
        }

    async def read_client_file(
        self, user_id: str, local_id: str
    ) -> tuple[bytes, dict[str, Any]] | None:
        name = sanitize_local_id(local_id)
        row = await self.db.fetch_one(
            "SELECT * FROM client_files WHERE user_id = ? AND local_id = ?", (user_id, name)
        )
        path = self._resolve_within(self.user_dir(user_id), f"client_{name}")
        if row is None or not path.is_file():
            return None
        return path.read_bytes(), {
            "id": name,
            "mime_type": row["mime_type"],
            "size": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "updated_at": int(row["updated_at"]),
        }

    async def delete_client_file(self, user_id: str, local_id: str) -> bool:
        name = sanitize_local_id(local_id)
        row = await self.db.fetch_one(
            "SELECT 1 FROM client_files WHERE user_id = ? AND local_id = ?", (user_id, name)
        )
        if row is None:
            return False
        await self.db.execute(
            "DELETE FROM client_files WHERE user_id = ? AND local_id = ?", (user_id, name)
        )
        self._resolve_within(self.user_dir(user_id), f"client_{name}").unlink(missing_ok=True)
        return True

    async def usage(self, user_id: str) -> tuple[int, int]:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS s FROM files"
            " WHERE user_id = ? AND deleted = 0",
            (user_id,),
        )
        if row is None:
            return 0, 0
        return int(row["c"] or 0), int(row["s"] or 0)


def sanitize_local_id(local_id: str) -> str:
    candidate = (local_id or "").strip()
    if not _SAFE_LOCAL_ID.match(candidate) or ".." in candidate:
        raise ApiError(400, "非法的附件 id", "invalid_request_error", param="local_id")
    return candidate


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def content_disposition(
    filename: str, mime_type: str = "", *, force_attachment: bool = False
) -> str:
    """RFC 6266 header that survives non-ASCII names (headers are latin-1).

    Script-capable MIME types are forced to ``attachment`` so an uploaded HTML or
    SVG file can never execute on the application origin.
    """
    from urllib.parse import quote

    disposition = "attachment" if force_attachment else "inline"
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip() or "file"
    ascii_name = ascii_name.replace('"', "_")
    return (
        f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    )


@router.post("/anthropic/v1/files", response_model=FileObject)
async def upload_file(request: Request) -> FileObject:
    user = await authenticate(request)
    store: FileStore = request.app.state.files
    reject_oversized_content_length(
        request, request.app.state.settings.max_upload_bytes + 2 * 1024 * 1024
    )
    form = await request.form()
    upload = form.get("file")
    purpose = form.get("purpose")
    if not isinstance(upload, StarletteUploadFile):
        raise ApiError(400, "缺少 multipart 字段 file", "invalid_request_error", param="file")
    record = await store.save_upload(
        user.id, upload, purpose if isinstance(purpose, str) else None
    )
    await form.close()
    return record.to_object()


@router.get("/anthropic/v1/files/{file_id}", response_model=FileObject)
async def get_file_metadata(file_id: str, request: Request) -> FileObject:
    user = await authenticate(request)
    store: FileStore = request.app.state.files
    record = await store.metadata(user.id, file_id)
    if record is None:
        raise not_found(f"文件不存在：{file_id}")
    return record.to_object()


@router.get("/anthropic/v1/files/{file_id}/content")
async def get_file_content(file_id: str, request: Request):
    from fastapi.responses import Response

    user = await authenticate(request)
    store: FileStore = request.app.state.files
    data, record = await store.read(user.id, file_id)
    return Response(
        content=data,
        media_type=record.mime_type,
        headers=file_response_headers(record.mime_type, record.filename),
    )


@router.delete("/anthropic/v1/files/{file_id}", response_model=FileDeleted)
async def delete_file(file_id: str, request: Request) -> FileDeleted:
    user = await authenticate(request)
    store: FileStore = request.app.state.files
    deleted = await store.delete(user.id, file_id)
    if not deleted:
        raise not_found(f"文件不存在：{file_id}")
    return FileDeleted(id=file_id)
