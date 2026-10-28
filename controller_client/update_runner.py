"""The self-update workflow (SELF_UPDATE_V1), apart from the socket.

Old runner: ``prepare_update`` validates an ``update_runner`` message before
anything is accepted, then ``RunnerUpdate.run`` verifies and extracts the
bundle into a new sibling folder, builds its venv, starts it detached and waits
for it to take over (the server supersedes this connection, or the new
runner's marker file appears). The whole run is bounded by
``UPDATE_MAX_SECONDS``.

New runner: ``finish_update_as_new_runner`` runs after its first accepted
handshake: it writes the marker and the start script and deletes the old
install.

Status messages go out through the ``StatusSender`` the client passes in, so
this module never touches the connection.
"""

import asyncio
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

from controller_client.config import ClientConfig
from controller_client.exceptions import UpdateError
from controller_client.protocol import (
    CLIENT_VERSION,
    UPDATE_MAX_SECONDS,
    UpdatePhase,
    UpdateRunnerPayload,
    parse_update_runner_payload,
    version_tuple,
)
from controller_client.update_archive import verify_and_extract
from controller_client.update_cleanup import remove_old_install
from controller_client.update_handover import (
    SpawnedRunner,
    is_handed_over,
    is_running,
    mark_connected,
    spawn_new_runner,
    terminate_runner,
)
from controller_client.update_installer import install
from controller_client.update_layout import (
    RUNNING_PACKAGE_DIR,
    UpdateLayout,
    base_for_install_root,
    check_free_space,
    install_env_file,
    plan_layout,
    write_start_script,
)

logger = logging.getLogger(__name__)

# (update_id, phase, detail): sends one ``update_status``.
StatusSender = Callable[[str, UpdatePhase, str], Awaitable[None]]

# How long the old runner waits for the new one to take over before it reports
# failed and stops the process it started. The installer's step limits plus
# this stay inside UPDATE_MAX_SECONDS.
HANDOVER_TIMEOUT_SECONDS: Final[float] = 300.0
_HANDOVER_POLL_SECONDS: Final[float] = 2.0


@dataclass(frozen=True)
class Handover:
    """Set once the new runner was spawned."""

    version: str
    new_root: Path


def prepare_update(
    data: Mapping[str, object], old_package_dir: Path = RUNNING_PACKAGE_DIR
) -> tuple[UpdateRunnerPayload, UpdateLayout]:
    """Validate an ``update_runner`` before it is accepted. Blocking (disk).

    Raises ProtocolError for an invalid message and UpdateError for an update
    this runner refuses (not newer, source checkout, not enough disk space).
    """
    payload = parse_update_runner_payload(dict(data))
    target = version_tuple(payload.target_version)
    own = version_tuple(CLIENT_VERSION)
    if target is None or own is None or target <= own:
        raise UpdateError(
            f"This Test Runner already runs version {CLIENT_VERSION}, which is "
            f"not older than {payload.target_version}"
        )
    layout = plan_layout(old_package_dir, payload.target_version)
    check_free_space(layout.base)
    return payload, layout


class RunnerUpdate:
    """One accepted ``update_runner``, run by the old runner."""

    def __init__(
        self,
        payload: UpdateRunnerPayload,
        layout: UpdateLayout,
        config: ClientConfig,
        send_status: StatusSender,
        old_package_dir: Path = RUNNING_PACKAGE_DIR,
    ) -> None:
        self._payload = payload
        self._layout = layout
        self._config = config
        self._send_status = send_status
        self._old_package_dir = old_package_dir
        # The extracted new folder, once it exists (removed again on failure
        # unless the new runner was already started from it).
        self._new_root: Path | None = None
        self._spawned: SpawnedRunner | None = None
        self.handover: Handover | None = None

    @property
    def update_id(self) -> str:
        return self._payload.update_id

    async def run(self) -> bool:
        """Run the update; True once the new runner took over.

        On failure (a step, the handover, or the UPDATE_MAX_SECONDS deadline)
        it reports ``failed`` and returns False; this runner keeps running.
        """
        deadline = asyncio.timeout(UPDATE_MAX_SECONDS)
        try:
            async with deadline:
                await self._install_and_hand_over()
            return True
        except asyncio.CancelledError:
            # Stopping: a spawned runner is left alone, an unfinished folder
            # is removed.
            if self._spawned is None and self._new_root is not None:
                shutil.rmtree(self._new_root, ignore_errors=True)
            raise
        except Exception as e:
            if deadline.expired():
                detail = await self._deadline_detail()
                if detail is None:
                    return True
            elif isinstance(e, UpdateError):
                detail = str(e)
                logger.error("Update to %s failed: %s", self._target, e)
            else:
                detail = f"Unexpected error: {e!r}"
                logger.exception("Update to %s failed", self._target)
        await self._fail(detail)
        return False

    @property
    def _target(self) -> str:
        return self._payload.target_version

    async def _install_and_hand_over(self) -> None:
        layout = self._layout
        await self._status(UpdatePhase.PREPARING)
        bundle = await asyncio.to_thread(
            verify_and_extract, self._payload, layout.new_root
        )
        self._new_root = bundle.root
        cli_api_key = self._config.api_key if self._config.api_key_from_cli else None
        await asyncio.to_thread(
            install_env_file, self._old_package_dir, bundle.package_dir, cli_api_key
        )
        new_python = await install(layout, Path(sys.executable), self._on_phase)
        spawned = await asyncio.to_thread(
            spawn_new_runner, new_python, layout.new_root, self._new_runner_args()
        )
        self._spawned = spawned
        self.handover = Handover(version=self._target, new_root=layout.new_root)
        await self._status(
            UpdatePhase.STARTING,
            f"Started the new Test Runner from {layout.new_root} (pid {spawned.pid})",
        )
        logger.info(
            "The new Test Runner %s runs from %s (pid %d), log: %s. Stop it "
            "with Disconnect test machine on the panel%s.",
            self._target,
            layout.new_root,
            spawned.pid,
            spawned.log_path,
            "" if sys.platform == "win32" else f" or kill {spawned.pid}",
        )
        await self._status(
            UpdatePhase.WAITING_FOR_NEW_RUNNER, f"Log: {spawned.log_path}"
        )
        await self._wait_for_handover(spawned)

    async def _on_phase(self, phase: UpdatePhase) -> None:
        await self._status(phase)

    async def _status(self, phase: UpdatePhase, detail: str = "") -> None:
        await self._send_status(self.update_id, phase, detail)

    def _new_runner_args(self) -> list[str]:
        return [
            *self._config.cli_overrides,
            "--update-id",
            self.update_id,
            "--replaces-pid",
            str(os.getpid()),
            "--replaces-pid-created",
            repr(psutil.Process().create_time()),
            "--remove-old-install",
            str(self._layout.old_target),
        ]

    async def _wait_for_handover(self, spawned: SpawnedRunner) -> None:
        """Return once the new runner connected; raise UpdateError if it did not."""
        new_root = self._layout.new_root
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDOVER_TIMEOUT_SECONDS
        while not await asyncio.to_thread(is_handed_over, new_root, self.update_id):
            if not await asyncio.to_thread(is_running, spawned):
                if await asyncio.to_thread(is_handed_over, new_root, self.update_id):
                    return
                raise UpdateError(
                    "The new Test Runner exited before it connected; its folder "
                    f"{new_root} is kept, see {spawned.log_path}"
                )
            if loop.time() >= deadline:
                # One last look: the new runner may have connected since the
                # poll above; stopping it then would leave no runner at all.
                if await asyncio.to_thread(is_handed_over, new_root, self.update_id):
                    return
                await asyncio.to_thread(terminate_runner, spawned)
                if await asyncio.to_thread(is_handed_over, new_root, self.update_id):
                    # Connected just before it was stopped; report that.
                    raise UpdateError(
                        "The new Test Runner connected too late and was stopped; "
                        f"its folder {new_root} is kept, see {spawned.log_path}"
                    )
                raise UpdateError(
                    "The new Test Runner did not connect within "
                    f"{HANDOVER_TIMEOUT_SECONDS / 60:.0f} minutes and was stopped; "
                    f"its folder {new_root} is kept, see {spawned.log_path}"
                )
            await asyncio.sleep(_HANDOVER_POLL_SECONDS)

    async def _deadline_detail(self) -> str | None:
        """The failure detail after UPDATE_MAX_SECONDS; None if it handed over."""
        limit = f"{UPDATE_MAX_SECONDS / 60:.0f} minutes"
        spawned = self._spawned
        if spawned is None:
            logger.error("Update to %s did not finish within %s", self._target, limit)
            return f"The update did not finish within {limit}"
        new_root = self._layout.new_root
        if await asyncio.to_thread(is_handed_over, new_root, self.update_id):
            return None
        await asyncio.to_thread(terminate_runner, spawned)
        logger.error(
            "Update to %s: the new Test Runner did not connect within %s; stopped it",
            self._target,
            limit,
        )
        if await asyncio.to_thread(is_handed_over, new_root, self.update_id):
            return (
                "The new Test Runner connected too late and was stopped; its "
                f"folder {new_root} is kept, see {spawned.log_path}"
            )
        return (
            f"The update did not finish within {limit}; the new Test Runner was "
            f"stopped, its folder {new_root} is kept, see {spawned.log_path}"
        )

    async def _fail(self, detail: str) -> None:
        if self._spawned is None and self._new_root is not None:
            await asyncio.to_thread(shutil.rmtree, self._new_root, True)
        self.handover = None
        await self._status(UpdatePhase.FAILED, detail)


async def finish_update_as_new_runner(
    config: ClientConfig,
    update_id: str,
    send_status: StatusSender,
    install_root: Path = RUNNING_PACKAGE_DIR.parent,
) -> None:
    """The new runner's bookkeeping after its first accepted handshake."""
    try:
        await asyncio.to_thread(mark_connected, install_root, update_id)
        script = await asyncio.to_thread(
            write_start_script, base_for_install_root(install_root), install_root
        )
        logger.info("Start this Test Runner next time with %s", script)
    except OSError as e:
        logger.warning("Could not finish the update bookkeeping: %s", e)
    target = config.remove_old_install
    if target is None:
        return
    removed = await asyncio.to_thread(
        remove_old_install,
        Path(target),
        install_root,
        config.replaces_pid,
        config.replaces_pid_created,
    )
    phase = UpdatePhase.OLD_REMOVED if removed else UpdatePhase.OLD_REMOVAL_FAILED
    await send_status(update_id, phase, target)
