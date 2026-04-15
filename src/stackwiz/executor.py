"""Run bash install scripts on the host, streaming stdout/stderr line-by-line.

Two execution strategies share one env-prep + log-pump path:

- ``nsenter``: used when stackwiz runs inside its container (the common case).
  Every install step is piped to ``bash -s`` inside host PID 1's namespaces,
  so the script sees the host's systemd, apt, docker, k3s, etc.
- ``direct``: exec the script in the current namespace. Used when stackwiz
  itself runs on the host (dev workflow, CI without containers, unit tests).

Both modes are first-class. Strategy selection order:

1. explicit ``mode=`` kwarg to ``Executor(...)``
2. ``STACKWIZ_EXECUTOR_MODE`` env var
3. auto-detect: ``nsenter`` if the binary is on PATH, else ``direct``
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_VALID_MODES = ("nsenter", "direct")
_log = logging.getLogger("stackwiz.executor")


@dataclass
class StepResult:
    exit_code: int
    stdout_tail: str  # last ~4 KB, for the summary screen

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _resolve_mode(requested: str | None) -> str:
    mode = requested or os.environ.get("STACKWIZ_EXECUTOR_MODE")
    if mode is None:
        mode = "nsenter" if shutil.which("nsenter") else "direct"
    if mode not in _VALID_MODES:
        raise ValueError(
            f"invalid executor mode {mode!r}; expected one of {_VALID_MODES}"
        )
    return mode


class Executor:
    """Run bash scripts on the host, streaming stdout/stderr line-by-line."""

    def __init__(
        self,
        manifest_dir: Path,
        env_defaults: Mapping[str, str] | None = None,
        mode: str | None = None,
    ) -> None:
        self.manifest_dir = Path(manifest_dir)
        self.env_defaults = dict(env_defaults or {})
        self.mode = _resolve_mode(mode)
        _log.info("executor mode=%s manifest_dir=%s", self.mode, self.manifest_dir)

    def _build_command(self) -> list[str]:
        """Command that reads a bash script from stdin.

        nsenter: script piped to ``bash -s`` inside host PID 1's namespaces
        (the container's ``/manifest`` mount isn't visible to the host mount
        namespace, so we always pipe rather than exec a path).

        direct: ``bash -s`` in the current namespace.
        """
        if self.mode == "direct":
            return ["bash", "-s"]
        return [
            "nsenter",
            "--target", "1",
            "--mount", "--uts", "--ipc", "--net", "--pid",
            "--",
            "bash", "-s",
        ]

    def _build_script_text(self, script: Path, extra_env: Mapping[str, str]) -> str:
        """Prepend a `set -euo pipefail` + env assignments to the script body."""
        script_path = (
            script if script.is_absolute() else (self.manifest_dir / script)
        )
        body = script_path.read_text(encoding="utf-8")
        header_lines = ["set -euo pipefail"]
        for k, v in sorted(extra_env.items()):
            header_lines.append(f"export {k}={shlex.quote(v)}")
        header = "\n".join(header_lines) + "\n"
        return header + body

    async def run(
        self,
        script: Path,
        extra_env: Mapping[str, str] | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Run a script and yield `(stream, line)` tuples.

        `stream` is `"stdout"` or `"stderr"`. The final yield is always
        `("exit", str(exit_code))`. Cancellation of the consuming task kills
        the subprocess.
        """
        env = {**os.environ, **self.env_defaults, **(extra_env or {})}
        cmd = self._build_command()
        script_text = self._build_script_text(script, extra_env or {})
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(script_text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        assert proc.stdout is not None and proc.stderr is not None

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def pump(stream: asyncio.StreamReader, label: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                await queue.put((label, line.decode("utf-8", errors="replace").rstrip("\n")))
            await queue.put(None)  # sentinel

        pumps = [
            asyncio.create_task(pump(proc.stdout, "stdout")),
            asyncio.create_task(pump(proc.stderr, "stderr")),
        ]

        try:
            remaining = len(pumps)
            while remaining:
                item = await queue.get()
                if item is None:
                    remaining -= 1
                    continue
                yield item
            rc = await proc.wait()
            yield ("exit", str(rc))
        except asyncio.CancelledError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            raise
        finally:
            for t in pumps:
                if not t.done():
                    t.cancel()

    async def run_collect(
        self,
        script: Path,
        extra_env: Mapping[str, str] | None = None,
        on_line: Callable[[str, str], None] | None = None,
    ) -> StepResult:
        """Run a script, optionally call `on_line(stream, text)`, return a StepResult."""
        tail: list[str] = []
        exit_code = 1
        async for stream, text in self.run(script, extra_env):
            if stream == "exit":
                exit_code = int(text)
            else:
                if on_line is not None:
                    on_line(stream, text)
                tail.append(text)
                if len(tail) > 200:
                    tail.pop(0)
        return StepResult(exit_code=exit_code, stdout_tail="\n".join(tail))
