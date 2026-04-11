"""Run bash install scripts on the host via nsenter.

The installer container runs with `--privileged --pid=host --network=host`. Every
install step is wrapped in `nsenter --target 1 --all -- bash -c '...'` so the
script sees the host's systemd, apt, docker, k3s, etc. The Python process stays
isolated in the container.

When running outside a container (dev mode on Linux where you are already
PID 1's namespace, or during unit tests) set `STACKWIZ_EXECUTOR_MODE=direct` to
skip nsenter and exec the script in the current namespace.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StepResult:
    exit_code: int
    stdout_tail: str  # last ~4 KB, for the summary screen

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


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
        self.mode = mode or os.environ.get("STACKWIZ_EXECUTOR_MODE", "nsenter")

    def _build_command(self) -> list[str]:
        """Command that reads a bash script from stdin.

        In nsenter mode the script is piped to `bash -s` running inside host
        PID 1's namespaces — this sidesteps the fact that the container's
        mounted /manifest directory isn't visible from the host's mount
        namespace.
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
