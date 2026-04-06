from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import emoji
import xxhash

from .fs import PRIVATE_FILE_MODE, ensure_permissions, ensure_private_dir, write_private_text

STATE_FILENAME = ".grugnotes.json"
PROMPT_META_FILENAME = ".prompt.json"
DATE_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def absolute_path_no_resolve(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def assert_no_symlink_components(path: Path, *, label: str) -> None:
    absolute_path = path if path.is_absolute() else absolute_path_no_resolve(path)
    resolved_path = Path(os.path.realpath(os.fspath(absolute_path)))

    if resolved_path == absolute_path:
        return

    # macOS commonly exposes /private-backed paths through /var, /tmp, and /etc.
    # Treat that alias as benign, but reject all other path redirections.
    if sys.platform == "darwin":
        absolute_str = os.fspath(absolute_path)
        resolved_str = os.fspath(resolved_path)
        if resolved_str == f"/private{absolute_str}" or absolute_str == f"/private{resolved_str}":
            return

    raise ValueError(
        f"{label} cannot be a symlink or contain symlinked path components: {absolute_path}"
    )


def _django_ascii_slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^\w\s-]", "", normalized.lower())
    return re.sub(r"[-\s]+", "-", normalized).strip("-_")


def slugify_text(value: str) -> str:
    normalized = (value or "").strip()
    slug = _django_ascii_slugify(emoji.demojize(normalized))
    if slug:
        return slug

    # Avoid hidden/empty filenames (".md") for symbol/emoji-only titles.
    if not normalized:
        return "untitled"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"untitled-{digest}"


def title_from_slug_filename(filename: str) -> str:
    base = Path(filename).stem
    words = [part for part in re.split(r"[-_]+", base) if part]
    return " ".join(word.capitalize() for word in words)


def content_hash(text: str) -> str:
    return xxhash.xxh64(text.encode("utf-8"), seed=0).hexdigest()


def prompt_meta_dict(prompt_data: dict) -> dict:
    return {
        "slug": prompt_data.get("slug", ""),
        "name": prompt_data.get("prompt", ""),
        "emoji": prompt_data.get("emoji", ""),
        "mode": prompt_data.get("prompt_mode", "journal"),
    }


def _strip_front_matter(text: str) -> tuple[str | None, str]:
    if not text.startswith("---"):
        return None, text

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text

    front_matter_lines: list[str] = [lines[0]]
    for idx in range(1, len(lines)):
        line = lines[idx]
        front_matter_lines.append(line)
        if line.strip() == "---":
            remainder = "".join(lines[idx + 1 :])
            return "".join(front_matter_lines), remainder

    return None, text


def _legacy_content_hash(text: str) -> str:
    _front_matter, body = _strip_front_matter(text)
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def matches_synced_hash(text: str, synced_hash: str) -> tuple[bool, str, bool]:
    local_hash = content_hash(text)
    if local_hash == synced_hash:
        return True, local_hash, False

    if synced_hash.startswith("sha256:") and _legacy_content_hash(text) == synced_hash:
        # Legacy state entry is semantically unchanged; caller can migrate to xxhash.
        return True, local_hash, True

    return False, local_hash, False


@dataclass
class FileEntry:
    block_id: int
    save_count: int
    synced_hash: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FileEntry":
        return cls(
            block_id=int(payload["block_id"]),
            save_count=int(payload.get("save_count", 0)),
            synced_hash=str(payload.get("synced_hash", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "save_count": self.save_count,
            "synced_hash": self.synced_hash,
        }


@dataclass
class SyncState:
    sync_dir: Path
    version: int = 2
    scope: dict[str, str] | None = None
    last_synced_at: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    files: dict[str, FileEntry] = field(default_factory=dict)
    legacy_deleted_paths: set[str] = field(default_factory=set, repr=False)

    @property
    def path(self) -> Path:
        return self.sync_dir / STATE_FILENAME

    @property
    def shadow_dir(self) -> Path:
        return self.sync_dir / ".grugnotes" / "shadows"

    @property
    def trash_dir(self) -> Path:
        return self.sync_dir / ".grugnotes" / "trash"

    @property
    def scope_prompt(self) -> str | None:
        if not self.scope:
            return None
        return self.scope.get("prompt")

    @classmethod
    def load(cls, sync_dir: str | Path) -> "SyncState":
        path = absolute_path_no_resolve(sync_dir)
        assert_no_symlink_components(path, label="Sync directory")
        path.mkdir(parents=True, exist_ok=True)
        state_path = path / STATE_FILENAME

        if not state_path.exists():
            state = cls(sync_dir=path)
            state.save()
            return state
        if state_path.is_symlink():
            raise ValueError("Sync state file cannot be a symlink.")

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        scope = payload.get("scope")
        scope_prompt = scope.get("prompt") if isinstance(scope, dict) else None
        files_payload = payload.get("files", {})
        files: dict[str, FileEntry] = {}
        legacy_deleted_paths: set[str] = set()
        for rel_path, entry_payload in files_payload.items():
            validate_note_rel_path(rel_path, scoped_prompt=scope_prompt)
            files[rel_path] = FileEntry.from_dict(entry_payload)
            if bool(entry_payload.get("deleted_remotely", False)):
                legacy_deleted_paths.add(rel_path)
        return cls(
            sync_dir=path,
            version=int(payload.get("version", 1)),
            scope=scope,
            last_synced_at=payload.get("last_synced_at"),
            api_key=payload.get("api_key") or None,
            base_url=payload.get("base_url") or None,
            files=files,
            legacy_deleted_paths=legacy_deleted_paths,
        )

    def save(self) -> None:
        ensure_private_dir(self.sync_dir, stop_at=self.sync_dir)
        payload = {
            "version": self.version,
            "scope": self.scope,
            "last_synced_at": self.last_synced_at,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "files": {rel_path: entry.to_dict() for rel_path, entry in sorted(self.files.items())},
        }
        tmp_path = self.path.with_suffix(".json.tmp")
        write_private_text(
            tmp_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            stop_at=self.sync_dir,
        )
        tmp_path.replace(self.path)
        ensure_permissions(self.path, PRIVATE_FILE_MODE)

    def get_file(self, rel_path: str) -> FileEntry | None:
        return self.files.get(rel_path)

    def get_file_by_block_id(self, block_id: int) -> tuple[str, FileEntry] | None:
        for rel_path, entry in self.files.items():
            if entry.block_id == block_id:
                return rel_path, entry
        return None

    def set_file(
        self,
        rel_path: str,
        block_id: int,
        save_count: int,
        synced_hash: str,
    ) -> FileEntry:
        entry = FileEntry(
            block_id=int(block_id),
            save_count=int(save_count),
            synced_hash=synced_hash,
        )
        self.files[rel_path] = entry
        return entry

    def move_file(self, old_rel_path: str, new_rel_path: str) -> None:
        if old_rel_path == new_rel_path:
            return
        entry = self.files.pop(old_rel_path, None)
        if entry is None:
            return
        self.files[new_rel_path] = entry

    def remove_file(self, rel_path: str) -> None:
        self.files.pop(rel_path, None)

    def has_unresolved_conflicts(self) -> bool:
        return any(self.sync_dir.rglob("*.conflict.md"))

    def _safe_subpath(self, root: Path, rel_path: str) -> Path:
        candidate = root / rel_path
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
        if not resolved_candidate.is_relative_to(resolved_root):
            raise ValueError(f"Path escapes root directory: {rel_path}")
        return candidate

    def _shadow_path(self, rel_path: str) -> Path:
        return self._safe_subpath(self.shadow_dir, rel_path)

    def _trash_path(self, rel_path: str, *, bucket: str) -> Path:
        return self._safe_subpath(self.trash_dir / bucket, rel_path)

    def write_shadow(self, rel_path: str, content: str) -> None:
        shadow_path = self._shadow_path(rel_path)
        write_private_text(shadow_path, content, stop_at=self.sync_dir)

    def read_shadow(self, rel_path: str) -> str | None:
        shadow_path = self._shadow_path(rel_path)
        if not shadow_path.exists():
            return None
        return shadow_path.read_text(encoding="utf-8", errors="replace")

    def remove_shadow(self, rel_path: str) -> None:
        shadow_path = self._shadow_path(rel_path)
        shadow_path.unlink(missing_ok=True)

    def move_shadow(self, old_rel_path: str, new_rel_path: str) -> None:
        if old_rel_path == new_rel_path:
            return
        old_shadow_path = self._shadow_path(old_rel_path)
        if not old_shadow_path.exists():
            return
        new_shadow_path = self._shadow_path(new_rel_path)
        ensure_private_dir(new_shadow_path.parent, stop_at=self.sync_dir)
        old_shadow_path.rename(new_shadow_path)

    def next_trash_path(self, rel_path: str) -> Path:
        bucket = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base_path = self._trash_path(rel_path, bucket=bucket)
        if not base_path.exists():
            return base_path

        parent = base_path.parent
        stem = base_path.stem
        suffix = base_path.suffix
        for index in range(1, 10000):
            candidate = parent / f"{stem}.{index}{suffix}"
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Could not allocate trash path for {rel_path}")


@dataclass(frozen=True)
class ParsedSyncPath:
    prompt_slug: str
    filename: str
    root_dir: str | None
    is_child: bool


def parse_sync_path(rel_path: str, *, scoped_prompt: str | None) -> ParsedSyncPath:
    path = Path(rel_path)
    parts = path.parts

    if scoped_prompt:
        if len(parts) == 1:
            return ParsedSyncPath(
                prompt_slug=scoped_prompt,
                filename=parts[0],
                root_dir=None,
                is_child=False,
            )
        if len(parts) == 2:
            return ParsedSyncPath(
                prompt_slug=scoped_prompt,
                filename=parts[1],
                root_dir=parts[0],
                is_child=True,
            )
        raise ValueError(f"Unsupported nested path for scoped sync: {rel_path}")

    if len(parts) == 2:
        return ParsedSyncPath(
            prompt_slug=parts[0],
            filename=parts[1],
            root_dir=None,
            is_child=False,
        )
    if len(parts) == 3:
        return ParsedSyncPath(
            prompt_slug=parts[0],
            filename=parts[2],
            root_dir=parts[1],
            is_child=True,
        )
    raise ValueError(f"Expected prompt directory in path: {rel_path}")


def path_to_prompt_and_name(rel_path: str, *, scoped_prompt: str | None) -> tuple[str, str]:
    parsed = parse_sync_path(rel_path, scoped_prompt=scoped_prompt)
    return parsed.prompt_slug, parsed.filename


def validate_note_rel_path(rel_path: str, *, scoped_prompt: str | None) -> None:
    normalized = (rel_path or "").strip()
    if not normalized:
        raise ValueError("Sync rel_path cannot be empty.")

    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ValueError(f"Sync rel_path must be relative: {rel_path}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Sync rel_path contains invalid traversal: {rel_path}")
    if any(part.startswith(".") for part in path.parts):
        raise ValueError(f"Sync rel_path cannot contain hidden path segments: {rel_path}")

    parsed = parse_sync_path(normalized, scoped_prompt=scoped_prompt)
    if not parsed.filename.endswith(".md"):
        raise ValueError(f"Sync rel_path must point to a markdown note: {rel_path}")
    if parsed.filename.endswith(".conflict.md"):
        raise ValueError(f"Conflict files cannot be tracked in sync state: {rel_path}")


def block_to_rel_path(
    prompt_slug: str,
    date_value: str | None,
    title: str | None,
    *,
    scoped_prompt: str | None,
    parent_stem: str | None = None,
) -> str:
    if title:
        base = slugify_text(title)
        filename = f"{base}.md"
    elif date_value:
        filename = f"{date_value}.md"
    else:
        raise ValueError("Block path requires a title or date.")

    relative_path = Path(parent_stem) / filename if parent_stem else Path(filename)
    if scoped_prompt:
        return str(relative_path)
    return str(Path(prompt_slug) / relative_path)
