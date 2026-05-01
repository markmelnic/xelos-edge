"""watchdog-backed FS watcher with per-path debounce + echo suppression."""

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

from .path_resolver import should_ignore

log = logging.getLogger(__name__)

# 1s settles editor save flurries (write-write-fsync) without feeling laggy.
DEBOUNCE_SECONDS = 1.0
# Window for ignoring events triggered by our own cloud→device writes.
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
        now = time.monotonic()
        until = self._suppressed.get(abs_path, 0.0)
        if until > now:
            return
        if until and until <= now:
            self._suppressed.pop(abs_path, None)
        # Bound the suppression dict on long-lived daemons.
        if len(self._suppressed) > 1024:
            stale = [k for k, v in self._suppressed.items() if v <= now]
            for k in stale:
                self._suppressed.pop(k, None)

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
        ev = FsEvent(abs_path=Path(abs_path), deleted=deleted)
        asyncio.create_task(self._on_event(ev))

    async def flush(self) -> None:
        """Fire pending debounced events immediately. Used at run-end so CC's
        final writes don't sit in the debounce window."""
        with self._lock:
            pending = list(self._pending.items())
            for abs_path, handle in pending:
                handle.cancel()
            self._pending.clear()
        # Walk pending out of the lock so handlers don't deadlock.
        tasks: list[asyncio.Task] = []
        for abs_path, _ in pending:
            ev = FsEvent(
                abs_path=Path(abs_path),
                deleted=not Path(abs_path).exists(),
            )
            tasks.append(asyncio.create_task(self._on_event(ev)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        # Move = delete(src) + write(dest).
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
        self._handler: _Debounced | None = None
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
        self._handler = handler
        log.info("fs watcher started on %s", self.root)

    async def flush(self) -> None:
        """Force any pending debounced writes through immediately."""
        if self._handler is not None:
            await self._handler.flush()

    async def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._observer = None
