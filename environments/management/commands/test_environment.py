from __future__ import annotations

import logging
from typing import Any

import docker
from django.core.management.base import BaseCommand

from environments.services import (
    build_environment_image,
    close_docker_client,
    create_container,
    ensure_environment_image,
    full_verification,
    get_docker_client,
    teardown_environment,
    wait_for_container_ready,
)
from environments.types import ContainerInfo


class Command(BaseCommand):
    help = "Test the environment by building image, creating container, and verifying services"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--no-cleanup",
            action="store_true",
            help="Skip container removal at the end (for debugging)",
        )
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Force rebuild the Docker image (ignore cache)",
        )

    def _log_skipped_cleanup(self, container_info: ContainerInfo) -> None:
        self.stdout.write(
            self.style.WARNING(f"\nSkipping cleanup. Container: {container_info.name}")
        )
        self.stdout.write(
            self.style.WARNING(
                f"To remove manually: docker rm -f {container_info.name}"
            )
        )

    def _remove_container(
        self, client: docker.DockerClient, container_info: ContainerInfo
    ) -> None:
        self.stdout.write("\nCleaning up...")
        try:
            teardown_environment(client, container_info.container_id)
            self.stdout.write(self.style.SUCCESS("Container removed"))
        except Exception as cleanup_error:
            self.stdout.write(self.style.ERROR(f"Cleanup error: {cleanup_error!s}"))

    def _cleanup_resources(
        self,
        client: docker.DockerClient | None,
        container_info: ContainerInfo | None,
        no_cleanup: bool,
    ) -> None:
        if container_info is None:
            return

        if no_cleanup:
            self._log_skipped_cleanup(container_info)
            return

        if client is None:
            return

        self._remove_container(client, container_info)

    def handle(self, *args: object, **options: object) -> None:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logging.getLogger("environments.services").setLevel(logging.INFO)

        no_cleanup = bool(options.get("no_cleanup", False))
        rebuild = bool(options.get("rebuild", False))

        client = None
        container_info: ContainerInfo | None = None

        try:
            self.stdout.write("Connecting to Docker...")
            client = get_docker_client()
            self.stdout.write(self.style.SUCCESS("Connected to Docker"))

            if rebuild:
                self.stdout.write("Building image (no cache)...")
                image_tag = build_environment_image(client, nocache=True)
            else:
                self.stdout.write("Ensuring image exists...")
                image_tag = ensure_environment_image(client)
            self.stdout.write(self.style.SUCCESS(f"Image ready: {image_tag}"))

            self.stdout.write("Creating container...")
            container_info = create_container(client, name_suffix="test")
            self.stdout.write(
                self.style.SUCCESS(f"Container created: {container_info.name}")
            )
            self.stdout.write(f"  SSH port:        {container_info.ports.ssh}")
            self.stdout.write(f"  VNC port:        {container_info.ports.vnc}")
            self.stdout.write(
                f"  Playwright port: {container_info.ports.playwright_cdp}"
            )

            self.stdout.write("Waiting for services to be ready...")
            health = wait_for_container_ready(container_info.ports)
            self.stdout.write(self.style.SUCCESS("All services are ready"))
            self.stdout.write(f"  SSH:        {health.ssh_ok}")
            self.stdout.write(f"  VNC:        {health.vnc_ok}")
            self.stdout.write(f"  Playwright: {health.playwright_ok}")

            self.stdout.write("\nRunning full verification...")
            result = full_verification(container_info)

            self.stdout.write("\nVerification Results:")
            self.stdout.write(
                self.style.SUCCESS(f"  ssh: passed")
                if result.ssh
                else self.style.ERROR(f"  ssh: FAILED")
            )
            self.stdout.write(
                self.style.SUCCESS(f"  vnc: passed")
                if result.vnc
                else self.style.ERROR(f"  vnc: FAILED")
            )
            self.stdout.write(
                self.style.SUCCESS(f"  playwright: passed")
                if result.playwright
                else self.style.ERROR(f"  playwright: FAILED")
            )

            self.stdout.write("")
            if result.all_passed:
                self.stdout.write(self.style.SUCCESS("All verification tests passed!"))
            else:
                self.stdout.write(self.style.ERROR("Some verification tests failed"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\nError during test: {e!s}"))
            raise

        finally:
            self._cleanup_resources(client, container_info, no_cleanup)
            if client is not None:
                close_docker_client(client)
                self.stdout.write("Docker client closed")
