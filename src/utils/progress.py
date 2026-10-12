"""Progress reporting and cancelling for the long-running parts of the engine.

The engine never prints progress itself (it has to work under the CLI and the desktop app
alike). A long loop gets a Stage and calls it with how far it is (0..1); the Stage passes the
overall fraction to the caller's callback and raises Cancelled if the caller asked to stop.

    stage = Stage(progress_cb, cancel_event, "Reading captions", start=0.0, span=0.45)
    for n, frame in enumerate(frames):
        stage(n / total)        # reports progress, and raises Cancelled when cancel_event is set
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

# (stage name, overall fraction 0..1)
ProgressCallback = Callable[[str, float], None]


class Cancelled(Exception):
    """The run was cancelled (cancel_event was set). Nothing is wrong."""


@dataclass
class Stage:
    callback: ProgressCallback | None = None
    cancel_event: threading.Event | None = None
    name: str = ""
    start: float = 0.0          # where this stage begins in the overall run, 0..1
    span: float = 1.0           # how much of the overall run it takes

    def __call__(self, fraction: float = 0.0) -> None:
        """Report that this stage is `fraction` done (0..1). Raises Cancelled when asked to stop."""
        self.check()
        if self.callback is not None:
            fraction = min(1.0, max(0.0, fraction))
            self.callback(self.name, round(min(1.0, self.start + self.span * fraction), 4))

    def check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled(f"cancelled during: {self.name or 'the run'}")


def silent() -> Stage:
    """For callers that don't want progress or cancelling (tests, one-off scripts)."""
    return Stage()
