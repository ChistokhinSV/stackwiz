"""Component-step pipeline shared by install() and uninstall().

Before this module, ``Engine.install`` and ``Engine.uninstall`` each owned
their own imperative loop — the shared skeleton (skip check, RUNNING
event, run the script, DONE event) was duplicated, and backend-adoption
hooks were scattered inline as ``if component.id == "consul"`` branches.

A ``ComponentStep`` carries a component + action + optional phase
callables. ``run_pipeline`` drives every step through the common phases:

    1. skip check            -> SKIPPED event, short-circuit
    2. RUNNING event
    3. execute (mint token, run script, revoke token)  [if ``script`` set]
    4. post_execute hook (async, for backend adoption)
    5. post_publish hook (register services, KV, policy)
    6. persist hook (mark_installed / mark_uninstalled)
    7. DONE event

Install and uninstall build different step lists from the manifest and
state plan; the scaffolding is identical.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from stackwiz.manifest import Component
from stackwiz.state import Action

if TYPE_CHECKING:
    from stackwiz.engine import Engine, StepEvent


# Phase callables — each optional. Kept as simple callables (no Protocol)
# so call sites can use lambdas / method references freely.
PrepareEnv = Callable[[], tuple[dict[str, str], str | None]]
"""Returns ``(env, install_token_or_None)``. Token is revoked after the script
runs (including on FAILED exit). Installers mint a scoped child token here;
uninstall passes through a no-op that returns ``({}, None)``."""

AsyncHook = Callable[[], Awaitable[None]]
SyncHook = Callable[[], None]


@dataclass
class ComponentStep:
    component: Component
    action: Action

    # Short-circuit: skip everything (including RUNNING/DONE), yield one
    # SKIPPED event, and move to the next step.
    skip: bool = False
    skip_message: str = ""

    # Script phase. When ``script`` is None the step has no script to run
    # (e.g. an uninstall for a component with no uninstall.sh).
    script: Path | None = None
    prepare: PrepareEnv | None = None

    # Post-script hooks.
    post_execute: AsyncHook | None = None  # async: for backend adoption probes
    post_publish: SyncHook | None = None   # services + KV + policy
    persist: SyncHook | None = None        # state file update

    done_message: str = "done"


async def run_pipeline(
    engine: Engine,
    steps: Iterable[ComponentStep],
) -> AsyncIterator[StepEvent]:
    """Drive each step through its phases, yielding StepEvents in real time.

    A FAILED event from the script phase aborts the whole pipeline (subsequent
    steps don't run) — identical semantics to the pre-pipeline engine loops.
    """
    # Imported lazily to avoid an engine <-> pipeline import cycle.
    from stackwiz.engine import Status, StepEvent

    for step in steps:
        if step.skip:
            yield StepEvent(
                step.component.id, Status.SKIPPED, step.action,
                message=step.skip_message,
            )
            continue

        yield StepEvent(
            step.component.id, Status.RUNNING, step.action,
            message=step.action.value,
        )

        if step.script is not None:
            env, token = ({}, None)
            if step.prepare is not None:
                env, token = step.prepare()
            try:
                failed = False
                async for event in engine._run_script(
                    step.component, step.action, step.script, env,
                ):
                    yield event
                    if event.status is Status.FAILED:
                        failed = True
                        break
                if failed:
                    return
            finally:
                if token and engine.vault is not None:
                    engine.vault.revoke_token(token)

        if step.post_execute is not None:
            await step.post_execute()
        if step.post_publish is not None:
            step.post_publish()
        if step.persist is not None:
            step.persist()

        yield StepEvent(
            step.component.id, Status.DONE, step.action,
            message=step.done_message,
        )
