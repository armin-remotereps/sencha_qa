from __future__ import annotations

import logging
from typing import Any

import docker
from django.core.management.base import BaseCommand

from agents.services.agent_loop import build_agent_config, run_agent
from agents.services.dmr_model_manager import ensure_model_available, warm_up_model
from agents.types import AgentStopReason
from environments.services import (
    close_docker_client,
    get_docker_client,
    provision_environment,
    teardown_environment,
)
from environments.types import ContainerInfo

DEFAULT_TASK = """
1. Install gnome calculator
2. Launch it
3. Do 2 + 2 on it
4. Make sure the result it's showing is 4
Note: use vnc click instead of key press
"""


class Command(BaseCommand):
    help = "Test the AI agent by provisioning a container and running a task"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--no-cleanup",
            action="store_true",
            help="Skip container removal at the end (for debugging)",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="Override the DMR model (e.g., 'ai/mistral')",
        )
        parser.add_argument(
            "--vision-model",
            type=str,
            default=None,
            help="Override the vision model (e.g., 'ai/qwen3-vl')",
        )
        parser.add_argument(
            "--task",
            type=str,
            default=None,
            help="Custom task description for the agent",
        )
        parser.add_argument(
            "--max-iterations",
            type=int,
            default=None,
            help="Override max iterations for the agent",
        )

    def _cleanup_resources(
        self,
        client: docker.DockerClient | None,
        container_info: ContainerInfo | None,
        no_cleanup: bool,
    ) -> None:
        if container_info is None:
            return

        if no_cleanup:
            self.stdout.write(
                self.style.WARNING(
                    f"\nSkipping cleanup. Container: {container_info.name}"
                )
            )
            self.stdout.write(
                self.style.WARNING(
                    f"To remove manually: docker rm -f {container_info.name}"
                )
            )
            return

        if client is None:
            return

        self.stdout.write("\nCleaning up...")
        try:
            teardown_environment(client, container_info.container_id)
            self.stdout.write(self.style.SUCCESS("Container removed"))
        except Exception as cleanup_error:
            self.stdout.write(self.style.ERROR(f"Cleanup error: {cleanup_error!s}"))

    def handle(self, *args: object, **options: object) -> None:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logging.getLogger("agents").setLevel(logging.INFO)
        logging.getLogger("environments").setLevel(logging.INFO)

        no_cleanup = bool(options.get("no_cleanup", False))
        model = options.get("model")
        model_str: str | None = str(model) if model is not None else None
        vision_model = options.get("vision_model")
        vision_model_str: str | None = (
            str(vision_model) if vision_model is not None else None
        )
        task = options.get("task")
        task_str: str = str(task) if task is not None else DEFAULT_TASK
        max_iterations = options.get("max_iterations")

        client = None
        container_info: ContainerInfo | None = None

        try:
            self.stdout.write("Connecting to Docker...")
            client = get_docker_client()
            self.stdout.write(self.style.SUCCESS("Connected to Docker"))

            self.stdout.write("Provisioning environment...")
            container_info = provision_environment(client, name_suffix="agent-test")
            self.stdout.write(
                self.style.SUCCESS(f"Environment ready: {container_info.name}")
            )
            self.stdout.write(f"  SSH port:        {container_info.ports.ssh}")
            self.stdout.write(f"  VNC port:        {container_info.ports.vnc}")
            self.stdout.write(
                f"  Playwright port: {container_info.ports.playwright_cdp}"
            )

            config = build_agent_config(model=model_str, vision_model=vision_model_str)
            if max_iterations is not None:
                from dataclasses import replace

                max_iter_int = (
                    int(max_iterations)
                    if isinstance(max_iterations, (int, str))
                    else 30
                )
                config = replace(config, max_iterations=max_iter_int)

            self.stdout.write(f"\nAgent config:")
            self.stdout.write(f"  Model:          {config.dmr.model}")
            if config.vision_dmr is not None:
                self.stdout.write(f"  Vision model:   {config.vision_dmr.model}")
            self.stdout.write(f"  Max iterations: {config.max_iterations}")
            self.stdout.write(f"  Timeout:        {config.timeout_seconds}s")

            self.stdout.write("\nChecking model availability...")
            try:
                ensure_model_available(config.dmr)
                self.stdout.write(
                    self.style.SUCCESS(f"Action model ready: {config.dmr.model}")
                )
                if config.vision_dmr is not None:
                    ensure_model_available(config.vision_dmr)
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Vision model ready: {config.vision_dmr.model}"
                        )
                    )
            except Exception as model_error:
                self.stdout.write(
                    self.style.ERROR(f"Model not available: {model_error!s}")
                )
                raise

            self.stdout.write("\nWarming up models...")
            warm_up_model(config.dmr)
            self.stdout.write(
                self.style.SUCCESS(f"Action model warmed up: {config.dmr.model}")
            )
            if config.vision_dmr is not None:
                warm_up_model(config.vision_dmr)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Vision model warmed up: {config.vision_dmr.model}"
                    )
                )

            self.stdout.write(f"\nTask: {task_str[:100]}...")
            self.stdout.write("\nStarting agent...\n")

            result = run_agent(
                task_str,
                container_info.ports,
                config=config,
            )

            self.stdout.write(f"\nAgent Result:")
            self.stdout.write(f"  Stop reason: {result.stop_reason.value}")
            self.stdout.write(f"  Iterations:  {result.iterations}")
            self.stdout.write(f"  Messages:    {len(result.messages)}")

            if result.error:
                self.stdout.write(self.style.ERROR(f"  Error: {result.error}"))

            # Print the last assistant message as the summary
            for msg in reversed(result.messages):
                if msg.role == "assistant" and isinstance(msg.content, str):
                    self.stdout.write(f"\nAgent summary:\n{msg.content}")
                    break

            if result.stop_reason == AgentStopReason.TASK_COMPLETE:
                self.stdout.write(self.style.SUCCESS("\nAgent completed successfully!"))
            else:
                self.stdout.write(
                    self.style.WARNING(f"\nAgent stopped: {result.stop_reason.value}")
                )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\nError: {e!s}"))
            raise

        finally:
            self._cleanup_resources(client, container_info, no_cleanup)
            if client is not None:
                close_docker_client(client)
                self.stdout.write("Docker client closed")
