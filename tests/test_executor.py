"""Unit tests for the executor — use direct mode so nsenter isn't required."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from stackwiz.executor import Executor

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash scripts aren't portable to native Windows; run in WSL/Linux/container",
)


@pytest.fixture
def manifest_dir(tmp_path: Path) -> Path:
    (tmp_path / "install").mkdir()
    (tmp_path / "install" / "hello.sh").write_text(
        "#!/usr/bin/env bash\n"
        'echo "hello from $NAME"\n'
        'echo "err" >&2\n'
        "exit 0\n"
    )
    (tmp_path / "install" / "fail.sh").write_text(
        "#!/usr/bin/env bash\nexit 7\n"
    )
    return tmp_path


async def test_run_success(manifest_dir: Path) -> None:
    os.environ["STACKWIZ_EXECUTOR_MODE"] = "direct"
    exe = Executor(manifest_dir=manifest_dir)
    lines: list[tuple[str, str]] = []

    def collect(stream: str, text: str) -> None:
        lines.append((stream, text))

    result = await exe.run_collect(
        Path("install/hello.sh"), extra_env={"NAME": "world"}, on_line=collect
    )
    assert result.ok
    assert ("stdout", "hello from world") in lines
    assert ("stderr", "err") in lines


async def test_run_failure(manifest_dir: Path) -> None:
    os.environ["STACKWIZ_EXECUTOR_MODE"] = "direct"
    exe = Executor(manifest_dir=manifest_dir)
    result = await exe.run_collect(Path("install/fail.sh"))
    assert not result.ok
    assert result.exit_code == 7
