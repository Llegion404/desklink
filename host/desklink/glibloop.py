"""Run a GLib main loop on a background thread so D-Bus + GStreamer integrate
with the asyncio networking loop."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from gi.repository import GLib


class GLibRunner:
    def __init__(self, aloop: asyncio.AbstractEventLoop):
        self.aloop = aloop
        self.loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._run, name="glib-main", daemon=True)
        self._started = threading.Event()

    def _run(self) -> None:
        self._started.set()
        self.loop.run()

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def stop(self) -> None:
        self.loop.quit()

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        def _idle():
            try:
                fn(*args)
            except Exception:
                import traceback
                traceback.print_exc()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_idle)

    async def call(self, fn: Callable[[], Any]) -> Any:
        """Run fn on the GLib thread and await its result from asyncio."""
        fut: asyncio.Future = self.aloop.create_future()

        def _run():
            try:
                result = fn()
                self.aloop.call_soon_threadsafe(_resolve, fut, result, None)
            except Exception as exc:  # noqa: BLE001
                self.aloop.call_soon_threadsafe(_resolve, fut, None, exc)

        def _resolve(f: asyncio.Future, result: Any, exc: Exception | None):
            if not f.done():
                if exc is not None:
                    f.set_exception(exc)
                else:
                    f.set_result(result)

        self.submit(_run)
        return await fut
