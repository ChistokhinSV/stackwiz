"""Structured logger that writes to /state/install.log and broadcasts to the TUI."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "[%(asctime)s] %(levelname)-5s %(short_name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S %d.%m.%Y"

# Rotation: 25 MB per file × 10 files = up to ~250 MB of history retained.
# Override via STACKWIZ_LOG_MAX_BYTES / STACKWIZ_LOG_BACKUPS for ops tuning.
_DEFAULT_LOG_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_LOG_BACKUPS = 10


class _ShortNameFilter(logging.Filter):
    """Trim stackwiz.engine → engine, stackwiz.script.consul → script.consul."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "stackwiz":
            record.short_name = "stackwiz"
        elif record.name.startswith("stackwiz."):
            record.short_name = record.name[len("stackwiz."):]
        else:
            record.short_name = record.name
        return True


def configure(state_dir: Path, mode: str | None = None, verbose: bool = False) -> logging.Logger:
    """Attach a file handler to <state_dir>/install.log.

    Called by both `InstallerApp.__init__` (TUI path) and `run_headless`
    (headless path) so install.log captures script output from either mode.
    The handler appends across runs; operators see a continuous forensic
    trail. TUI widgets attach their own handler on top via `attach_tui_sink`.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "install.log"

    root = logging.getLogger("stackwiz")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Avoid duplicate handlers on re-configure (happens when the same Python
    # process calls configure() twice, e.g. in tests).
    for handler in list(root.handlers):
        if getattr(handler, "_stackwiz", False):
            root.removeHandler(handler)

    # Filter is attached to the HANDLER, not the logger — ancestor-logger
    # filters are not re-applied to records propagating up from child loggers
    # (like stackwiz.engine or stackwiz.script.consul), but handler filters
    # run for every record the handler processes.
    max_bytes = int(os.environ.get("STACKWIZ_LOG_MAX_BYTES") or _DEFAULT_LOG_MAX_BYTES)
    backups = int(os.environ.get("STACKWIZ_LOG_BACKUPS") or _DEFAULT_LOG_BACKUPS)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8",
    )
    file_handler.addFilter(_ShortNameFilter())
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    file_handler._stackwiz = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    # Human-readable session marker so `tail install.log` is easy to parse
    # across multiple runs.
    mode_label = f" [{mode}]" if mode else ""
    root.info(
        "=" * 72,
    )
    root.info(
        "stackwiz session started%s at %s",
        mode_label,
        datetime.now(UTC).isoformat(timespec="seconds"),
    )
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
