"""Unit tests for the ComponentStep pipeline."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stackwiz.engine import Status
from stackwiz.pipeline import ComponentStep, run_pipeline
from stackwiz.state import Action


def _component(cid: str = "x") -> MagicMock:
    c = MagicMock()
    c.id = cid
    return c


@pytest.fixture
def engine() -> MagicMock:
    """Minimal engine stub that only needs _run_script + .vault."""
    e = MagicMock()
    e.vault = None

    async def _no_script(*_, **__):
        # Default: no script was run; script phase shouldn't invoke this.
        if False:
            yield None
    e._run_script = _no_script
    return e


async def _collect(iterator) -> list:
    return [ev async for ev in iterator]


@pytest.mark.asyncio
async def test_skip_yields_single_skipped(engine: MagicMock) -> None:
    step = ComponentStep(
        component=_component("s1"), action=Action.NOOP,
        skip=True, skip_message="not selected",
    )
    events = await _collect(run_pipeline(engine, [step]))
    assert len(events) == 1
    assert events[0].status is Status.SKIPPED
    assert events[0].message == "not selected"


@pytest.mark.asyncio
async def test_no_script_runs_hooks_and_yields_done(engine: MagicMock) -> None:
    """When ``script=None``, pipeline should still run post_publish/persist
    (uninstall with no uninstall.sh still needs to deregister + mark state)."""
    post_publish = MagicMock()
    persist = MagicMock()
    step = ComponentStep(
        component=_component("no-script"),
        action=Action.UNINSTALL,
        script=None,
        post_publish=post_publish,
        persist=persist,
        done_message="removed",
    )
    events = await _collect(run_pipeline(engine, [step]))
    statuses = [e.status for e in events]
    assert statuses == [Status.RUNNING, Status.DONE]
    assert events[1].message == "removed"
    post_publish.assert_called_once()
    persist.assert_called_once()


@pytest.mark.asyncio
async def test_script_phase_failure_aborts_pipeline(engine: MagicMock) -> None:
    """A FAILED event from the script phase must stop the pipeline entirely."""
    from stackwiz.engine import StepEvent

    async def _failing_script(*_, **__):
        yield StepEvent(
            "s1", Status.FAILED, Action.INSTALL, message="exit 1", exit_code=1,
        )
    engine._run_script = _failing_script

    step1 = ComponentStep(
        component=_component("s1"), action=Action.INSTALL,
        script=Path("install/s1.sh"),
        prepare=lambda: ({}, None),
    )
    later_persist = MagicMock()
    step2 = ComponentStep(
        component=_component("s2"), action=Action.INSTALL,
        persist=later_persist,
    )
    events = await _collect(run_pipeline(engine, [step1, step2]))
    # First step: RUNNING -> FAILED. Second step never runs.
    assert [e.status for e in events] == [Status.RUNNING, Status.FAILED]
    later_persist.assert_not_called()


@pytest.mark.asyncio
async def test_token_revoked_even_on_failure(engine: MagicMock) -> None:
    """Scoped install tokens must be revoked even when the script fails."""
    from stackwiz.engine import StepEvent

    async def _failing_script(*_, **__):
        yield StepEvent("s", Status.FAILED, Action.INSTALL, message="boom", exit_code=2)
    engine._run_script = _failing_script
    vault = MagicMock()
    engine.vault = vault

    step = ComponentStep(
        component=_component("s"), action=Action.INSTALL,
        script=Path("install/s.sh"),
        prepare=lambda: ({"VAULT_TOKEN": "child"}, "child"),
    )
    await _collect(run_pipeline(engine, [step]))
    vault.revoke_token.assert_called_once_with("child")


@pytest.mark.asyncio
async def test_post_execute_runs_before_post_publish(engine: MagicMock) -> None:
    """Adopt-backend hooks must run before per-component registration."""
    calls: list[str] = []

    async def _adopt() -> None:
        calls.append("adopt")

    def _publish() -> None:
        calls.append("publish")

    def _persist() -> None:
        calls.append("persist")

    step = ComponentStep(
        component=_component("c"), action=Action.INSTALL,
        post_execute=_adopt,
        post_publish=_publish,
        persist=_persist,
    )
    await _collect(run_pipeline(engine, [step]))
    assert calls == ["adopt", "publish", "persist"]
