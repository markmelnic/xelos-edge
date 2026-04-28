"""Filesystem watcher — coalesces local changes and emits sync events.

Built on `watchdog` so it works on Linux (inotify), macOS (FSEvents),
and Windows (ReadDirectoryChangesW) without per-OS branching.

Events are debounced per absolute path so a sequence of
write-write-rename collapses into one sync event after a brief quiet
period. The handler is intentionally framework-agnostic: it calls a
callback the daemon owns, which decides whether to push, ignore (echo
of a cloud-originated write), or stash a conflict.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .path_resolver import resolve, should_ignore

log = logging.getLogger(__name__)

# Quiet window after the last event for a path before we treat it as
# settled. Most editors save in a write-write-fsync flurry that lasts
# tens of milliseconds; 1s is generous and still feels live.
DEBOUNCE_SECONDS = 1.0
# Suppression window for paths we just wrote ourselves (cloud → device
# echoes). Watchdog fires on our own writes; the hash check in the
# daemon also catches these but a path-level filter is cheaper.
ECHO_SUPPRESS_SECONDS = 5.0


@dataclass(slots=True)
class FsEvent:
    abs_path: Path
    deleted: bool


HandlerFn = Callable[[FsEvent], Awaitable[None]]


class _Debounced(FileSystemEventHandler):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        root: Path,
        on_event: HandlerFn,
        suppressed: dict[str, float],
    ) -> None:
        self._loop = loop
        self._root = root.resolve()
        self._on_event = on_event
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._lock = threading.Lock()
        self._suppressed = suppressed

    def _enqueue(self, abs_path: str, *, deleted: bool) -> None:
        # Echo-suppression: ignore events for paths we wrote ourselves
        # within the last few seconds.
        now = time.monotonic()
        until = self._suppressed.get(abs_path, 0.0)
        if until > now:
            return

        path = Path(abs_path)
        if should_ignore(path):
            return
        try:
            path.resolve().relative_to(self._root)
        except ValueError:
            return

        with self._lock:
            handle = self._pending.pop(abs_path, None)
            if handle is not None:
                handle.cancel()
            self._pending[abs_path] = self._loop.call_later(
                DEBOUNCE_SECONDS,
                self._fire,
                abs_path,
                deleted,
            )

    def _fire(self, abs_path: str, deleted: bool) -> None:
        with self._lock:
            self._pending.pop(abs_path, None)
        # call_later runs on the loop thread, so we can schedule a
        # coroutine directly.
        ev = FsEvent(abs_path=Path(abs_path), deleted=deleted)
        asyncio.create_task(self._on_event(ev))

    # watchdog event hooks ------------------------------------------------
    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path, deleted=False)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path, deleted=False)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path, deleted=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Treat move as delete(src) + write(dest); watchdog provides
        # both a `dest_path` and the original `src_path`.
        if not event.is_directory:
            self._enqueue(event.src_path, deleted=True)
            dest = getattr(event, "dest_path", None)
            if dest:
                self._enqueue(dest, deleted=False)


class FsWatcher:
    def __init__(
        self,
        *,
        root: Path,
        on_event: HandlerFn,
    ) -> None:
        self.root = root
        self._on_event = on_event
        self._observer: Observer | None = None
        self._suppressed: dict[str, float] = {}

    def suppress(self, abs_path: str | Path, seconds: float = ECHO_SUPPRESS_SECONDS) -> None:
        self._suppressed[str(abs_path)] = time.monotonic() + seconds

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        handler = _Debounced(
            loop=loop,
            root=self.root,
            on_event=self._on_event,
            suppressed=self._suppressed,
        )
        observer = Observer()
        observer.schedule(handler, str(self.root), recursive=True)
        observer.start()
        self._observer = observer
        log.info("fs watcher started on %s", self.root)

    async def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._observer = None
