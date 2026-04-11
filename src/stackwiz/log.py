"""Structured logger that writes to /state/install.log and broadcasts to the TUI."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

LOG_FORMAT = "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S %d.%m.%Y"


def configure(state_dir: Path, verbose: bool = False) -> logging.Logger:
    """Attach a file handler to /state/install.log.

    TUI widgets attach their own handler on top of this via `attach_tui_sink`.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "install.log"

    root = logging.getLogger("stackwiz")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Avoid duplicate handlers on re-configure
    for handler in list(root.handlers):
        if getattr(handler, "_stackwiz", False):
            root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    file_handler._stackwiz = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    root.info("stackwiz session started at %s", datetime.now(UTC).isoformat(timespec="seconds"))
    return root


def attach_tui_sink(callback: Callable[[str], None]) -> logging.Handler:
    """Pipe every stackwiz log record into a TUI widget via `callback(line)`."""
    handler = _CallbackHandler(callback)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    handler._stackwiz = True  # type: ignore[attr-defined]
    logging.getLogger("stackwiz").addHandler(handler)
    return handler


class _CallbackHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:  # noqa: BLE001 — never let logging crash the TUI
            pass
