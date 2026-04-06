from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

PushCallback = Callable[[set[Path]], Any]
PollCallback = Callable[[], Any]
IgnoreCallback = Callable[[Path], bool]
HashCallback = Callable[[str], str]
ErrorCallback = Callable[[Exception], Any]


class _SyncEventHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[Path], None]):
        super().__init__()
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._on_change(Path(event.src_path))
        dest_path = getattr(event, "dest_path", None)
        if dest_path:
            self._on_change(Path(dest_path))


class SyncWatcher:
    def __init__(
        self,
        *,
        sync_dir: Path,
        interval_seconds: int,
        on_push_paths: PushCallback,
        on_poll_remote: PollCallback,
        is_ignored: IgnoreCallback,
        hash_text: HashCallback,
        on_error: ErrorCallback | None = None,
        debounce_seconds: float = 3.0,
    ):
        self.sync_dir = sync_dir
        self.interval_seconds = interval_seconds
        self.on_push_paths = on_push_paths
        self.on_poll_remote = on_poll_remote
        self.is_ignored = is_ignored
        self.hash_text = hash_text
        self.on_error = on_error
        self.debounce_seconds = debounce_seconds

        self._observer = Observer()
        self._pending: set[Path] = set()
        self._last_change_at: float | None = None
        self._last_activity_at = time.monotonic()
        self._running = False
        self._just_written: dict[Path, str] = {}
        self._lock = threading.Lock()
        self._error_tasks: set[asyncio.Task[Any]] = set()

    @property
    def last_activity_at(self) -> float:
        with self._lock:
            return self._last_activity_at

    def mark_activity(self) -> None:
        with self._lock:
            self._last_activity_at = time.monotonic()

    def mark_just_written(self, file_path: Path, file_content: str) -> None:
        with self._lock:
            self._just_written[file_path.resolve()] = self.hash_text(file_content)

    def requeue_paths(self, paths: set[Path]) -> None:
        with self._lock:
            self._pending.update(paths)
            if paths:
                self._last_change_at = time.monotonic()

    async def run(self) -> None:
        self._running = True
        handler = _SyncEventHandler(self._on_file_changed)
        self._observer.schedule(handler, str(self.sync_dir), recursive=True)
        self._observer.start()

        try:
            await asyncio.gather(
                self._push_loop(),
                self._poll_loop(),
            )
        finally:
            self._running = False
            self._observer.stop()
            self._observer.join()

    def stop(self) -> None:
        self._running = False

    async def _call(self, callback: Callable[..., Any], *args) -> Any:
        result = callback(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    def _on_file_changed(self, file_path: Path) -> None:
        try:
            abs_path = file_path.resolve()
            if self.is_ignored(abs_path):
                return

            with self._lock:
                expected_hash = self._just_written.get(abs_path)
            if expected_hash is not None and abs_path.exists():
                local_hash = self.hash_text(abs_path.read_text(encoding="utf-8", errors="replace"))
                if local_hash == expected_hash:
                    with self._lock:
                        self._just_written.pop(abs_path, None)
                    return

            with self._lock:
                now = time.monotonic()
                self._pending.add(abs_path)
                self._last_change_at = now
                self._last_activity_at = now
        except Exception as exc:  # pragma: no cover - defensive guard for watch loop
            self._report_error(exc)

    async def _push_loop(self) -> None:
        while self._running:
            try:
                pending: set[Path] | None = None
                now = time.monotonic()
                with self._lock:
                    if self._pending and self._last_change_at is not None:
                        elapsed = now - self._last_change_at
                        if elapsed >= self.debounce_seconds:
                            pending = set(self._pending)
                            self._pending.clear()
                            self._last_change_at = None
                if pending:
                    await self._call(self.on_push_paths, pending)
                await asyncio.sleep(0.25)
            except Exception as exc:  # pragma: no cover - defensive guard for watch loop
                self._report_error(exc)
                await asyncio.sleep(1.0)

    def _poll_sleep_seconds(self, base_interval: float) -> float:
        jitter_ceiling = max(0.0, float(base_interval) * 0.25)
        if jitter_ceiling == 0.0:
            return float(base_interval)
        return float(base_interval) + random.uniform(0.0, jitter_ceiling)

    async def _poll_loop(self) -> None:
        next_sleep: float = self.interval_seconds
        while self._running:
            await asyncio.sleep(self._poll_sleep_seconds(next_sleep))
            next_sleep = self.interval_seconds
            if not self._running:
                return
            try:
                result = await self._call(self.on_poll_remote)
                if isinstance(result, (int, float)) and result > 0:
                    next_sleep = result
            except Exception as exc:  # pragma: no cover - defensive guard for watch loop
                self._report_error(exc)

    def _report_error(self, exc: Exception) -> None:
        if self.on_error is None:
            return
        result = self.on_error(exc)
        if isinstance(result, Awaitable):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            task = loop.create_task(result)
            self._error_tasks.add(task)
            task.add_done_callback(self._error_tasks.discard)
