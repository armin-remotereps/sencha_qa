from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import docker.errors
from django.test import TestCase

from environments.services import (
    build_environment_image,
    check_container_health,
    check_vnc_connection,
    close_docker_client,
    create_container,
    ensure_container_running,
    ensure_environment_image,
    full_verification,
    get_container_info,
    get_docker_client,
    image_exists,
    list_environment_containers,
    provision_environment,
    remove_container,
    teardown_environment,
    verify_vnc_service,
    wait_for_container_ready,
)
from environments.types import ContainerInfo, ContainerPorts, HealthCheckResult


class DockerClientTests(TestCase):

    @patch("environments.services.docker_client.docker.DockerClient")
    def test_creates_docker_client_with_host_from_settings(
        self, mock_docker_client: Mock
    ) -> None:
        mock_client = Mock()
        mock_docker_client.return_value = mock_client

        result = get_docker_client()

        self.assertEqual(result, mock_client)
        mock_docker_client.assert_called_once_with(
            base_url="unix:///var/run/docker.sock"
        )

    def test_closes_docker_client_connection(self) -> None:
        mock_client = Mock()
        close_docker_client(mock_client)
        mock_client.close.assert_called_once()


class ImageManagementTests(TestCase):

    def test_returns_true_when_image_exists(self) -> None:
        mock_client = Mock()
        mock_client.images.get.return_value = Mock()

        result = image_exists(mock_client)

        self.assertTrue(result)
        mock_client.images.get.assert_called_once_with("auto-tester-env:latest")

    def test_returns_false_when_image_not_found(self) -> None:
        mock_client = Mock()
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        result = image_exists(mock_client)

        self.assertFalse(result)

    @patch("environments.services.image.Path")
    def test_builds_environment_image_with_correct_parameters(
        self, mock_path: Mock
    ) -> None:
        mock_client = Mock()
        mock_dockerfile_path = Mock()
        mock_path.return_value.resolve.return_value.parent.parent = Mock()
        mock_path.return_value.resolve.return_value.parent.parent.__truediv__ = Mock(
            return_value=mock_dockerfile_path
        )
        mock_client.api.build.return_value = iter(
            [{"stream": "Step 1/5 : FROM ubuntu:24.04\n"}]
        )

        result = build_environment_image(mock_client, nocache=True)

        self.assertEqual(result, "auto-tester-env:latest")
        mock_client.api.build.assert_called_once()
        call_kwargs = mock_client.api.build.call_args[1]
        self.assertEqual(call_kwargs["tag"], "auto-tester-env:latest")
        self.assertEqual(
            call_kwargs["buildargs"],
            {"VNC_PASSWORD": "testpass123"},
        )
        self.assertTrue(call_kwargs["nocache"])
        self.assertTrue(call_kwargs["rm"])

    def test_builds_image_when_not_exists(self) -> None:
        mock_client = Mock()
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        with patch("environments.services.image.build_environment_image") as mock_build:
            mock_build.return_value = "auto-tester-env:latest"
            result = ensure_environment_image(mock_client)

        self.assertEqual(result, "auto-tester-env:latest")
        mock_build.assert_called_once_with(mock_client)

    def test_does_not_build_image_when_exists(self) -> None:
        mock_client = Mock()
        mock_client.images.get.return_value = Mock()

        with patch("environments.services.image.build_environment_image") as mock_build:
            result = ensure_environment_image(mock_client)

        self.assertEqual(result, "auto-tester-env:latest")
        mock_build.assert_not_called()


class ContainerLifecycleTests(TestCase):

    def test_creates_container_with_auto_generated_suffix(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_container.id = "container123"
        mock_container.name = "auto-tester-env-abc12345"
        mock_container.status = "running"
        mock_container.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32769"}],
        }

        mock_client.containers.create.return_value = mock_container

        result = create_container(mock_client, api_key="test-key")

        self.assertIsInstance(result, ContainerInfo)
        self.assertEqual(result.container_id, "container123")
        self.assertEqual(result.name, "auto-tester-env-abc12345")
        self.assertEqual(result.ports.vnc, 32769)
        mock_container.start.assert_called_once()
        mock_container.reload.assert_called_once()

    def test_creates_container_with_custom_suffix(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_container.id = "container456"
        mock_container.name = "auto-tester-env-custom"
        mock_container.status = "running"
        mock_container.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40001"}],
        }

        mock_client.containers.create.return_value = mock_container

        result = create_container(mock_client, name_suffix="custom", api_key="test-key")

        self.assertEqual(result.name, "auto-tester-env-custom")
        mock_client.containers.create.assert_called_once()
        call_kwargs = mock_client.containers.create.call_args[1]
        self.assertEqual(call_kwargs["name"], "auto-tester-env-custom")

    def test_gets_container_info_with_fresh_state(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_container.id = "container789"
        mock_container.name = "auto-tester-env-test"
        mock_container.status = "running"
        mock_container.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "50001"}],
        }

        mock_client.containers.get.return_value = mock_container

        result = get_container_info(mock_client, "container789")

        self.assertEqual(result.container_id, "container789")
        self.assertEqual(result.ports.vnc, 50001)
        mock_container.reload.assert_called_once()

    def test_removes_container_with_force(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_client.containers.get.return_value = mock_container

        remove_container(mock_client, "container123", force=True)

        mock_container.remove.assert_called_once_with(force=True)

    def test_ignores_not_found_error_when_removing_container(self) -> None:
        mock_client = Mock()
        mock_client.containers.get.side_effect = docker.errors.NotFound("not found")

        remove_container(mock_client, "nonexistent")

    def test_returns_existing_container_when_name_matches_and_running(
        self,
    ) -> None:
        mock_client = Mock()
        mock_existing = Mock()
        mock_existing.id = "existing123"
        mock_existing.name = "auto-tester-env-reuse"
        mock_existing.status = "running"
        mock_existing.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "55001"}],
        }

        mock_client.containers.get.return_value = mock_existing

        result = ensure_container_running(
            mock_client, name_suffix="reuse", api_key="test-key"
        )

        self.assertEqual(result.container_id, "existing123")
        self.assertEqual(result.ports.vnc, 55001)
        mock_existing.start.assert_not_called()
        mock_existing.reload.assert_called_once()
        mock_client.containers.create.assert_not_called()

    def test_starts_and_returns_existing_container_when_stopped(self) -> None:
        mock_client = Mock()
        mock_existing = Mock()
        mock_existing.id = "stopped456"
        mock_existing.name = "auto-tester-env-stopped"
        mock_existing.status = "exited"
        mock_existing.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "56001"}],
        }

        mock_client.containers.get.return_value = mock_existing

        result = ensure_container_running(
            mock_client, name_suffix="stopped", api_key="test-key"
        )

        self.assertEqual(result.container_id, "stopped456")
        mock_existing.start.assert_called_once()
        mock_existing.reload.assert_called_once()
        mock_client.containers.create.assert_not_called()

    def test_creates_container_when_none_exists_with_name(self) -> None:
        mock_client = Mock()
        mock_client.containers.get.side_effect = docker.errors.NotFound("not found")

        mock_container = Mock()
        mock_container.id = "new789"
        mock_container.name = "auto-tester-env-fresh"
        mock_container.status = "running"
        mock_container.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "57001"}],
        }
        mock_client.containers.create.return_value = mock_container

        result = ensure_container_running(
            mock_client, name_suffix="fresh", api_key="test-key"
        )

        self.assertEqual(result.container_id, "new789")
        mock_client.containers.create.assert_called_once()
        mock_container.start.assert_called_once()
        mock_container.reload.assert_called_once()

    def test_lists_all_environment_containers(self) -> None:
        mock_client = Mock()
        mock_container1 = Mock()
        mock_container1.id = "container1"
        mock_container1.name = "auto-tester-env-one"
        mock_container1.status = "running"
        mock_container1.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10001"}],
        }

        mock_container2 = Mock()
        mock_container2.id = "container2"
        mock_container2.name = "auto-tester-env-two"
        mock_container2.status = "exited"
        mock_container2.ports = {
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20001"}],
        }

        mock_container3 = Mock()
        mock_container3.name = "other-container"

        mock_client.containers.list.return_value = [
            mock_container1,
            mock_container2,
            mock_container3,
        ]

        result = list_environment_containers(mock_client)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "auto-tester-env-one")
        self.assertEqual(result[1].name, "auto-tester-env-two")


class HealthCheckTests(TestCase):

    @patch("environments.services.health_check._check_port")
    def test_returns_all_ports_ok_when_healthy(self, mock_check_port: Mock) -> None:
        mock_check_port.return_value = True
        ports = ContainerPorts(vnc=5900)

        result = check_container_health(ports)

        self.assertTrue(result.vnc)
        self.assertTrue(result.is_healthy)
        mock_check_port.assert_called_once()

    @patch("environments.services.health_check._check_port")
    def test_returns_failed_ports_when_not_healthy(self, mock_check_port: Mock) -> None:
        mock_check_port.return_value = False
        ports = ContainerPorts(vnc=5900)

        result = check_container_health(ports)

        self.assertFalse(result.vnc)
        self.assertFalse(result.is_healthy)

    @patch("environments.services.health_check.check_container_health")
    @patch("environments.services.health_check.time")
    def test_returns_when_container_becomes_ready(
        self, mock_time: Mock, mock_check_health: Mock
    ) -> None:
        ports = ContainerPorts(vnc=5900)

        mock_time.monotonic.side_effect = [0.0, 1.0, 2.0]
        mock_time.sleep = Mock()

        unhealthy = HealthCheckResult(vnc=False)
        healthy = HealthCheckResult(vnc=True)
        mock_check_health.side_effect = [unhealthy, healthy]

        result = wait_for_container_ready(ports, timeout=10, interval=1)

        self.assertTrue(result.is_healthy)
        self.assertEqual(mock_check_health.call_count, 2)

    @patch("environments.services.health_check.check_container_health")
    @patch("environments.services.health_check.time")
    def test_raises_timeout_error_when_not_ready(
        self, mock_time: Mock, mock_check_health: Mock
    ) -> None:
        ports = ContainerPorts(vnc=5900)

        mock_time.monotonic.side_effect = [0.0, 1.0, 2.0, 3.0]
        mock_time.sleep = Mock()

        unhealthy = HealthCheckResult(vnc=False)
        mock_check_health.return_value = unhealthy

        with self.assertRaises(TimeoutError) as context:
            wait_for_container_ready(ports, timeout=2, interval=1)

        self.assertIn("not ready after 2 seconds", str(context.exception))


class VNCTests(TestCase):

    @patch("environments.services.vnc.socket.create_connection")
    def test_returns_true_when_rfb_protocol_detected(
        self, mock_create_connection: Mock
    ) -> None:
        mock_socket = MagicMock()
        mock_socket.recv.return_value = b"RFB"
        mock_socket.__enter__.return_value = mock_socket
        mock_socket.__exit__.return_value = None
        mock_create_connection.return_value = mock_socket

        ports = ContainerPorts(vnc=5900)
        result = check_vnc_connection(ports)

        self.assertTrue(result)

    @patch("environments.services.vnc.socket.create_connection")
    def test_returns_false_when_connection_fails(
        self, mock_create_connection: Mock
    ) -> None:
        import socket

        mock_create_connection.side_effect = socket.error("Connection refused")

        ports = ContainerPorts(vnc=5900)
        result = check_vnc_connection(ports)

        self.assertFalse(result)


class OrchestrationTests(TestCase):

    @patch("environments.services.orchestration.wait_for_container_ready")
    @patch("environments.services.orchestration.ensure_container_running")
    @patch("environments.services.orchestration.ensure_environment_image")
    def test_provisions_environment_with_all_steps(
        self,
        mock_ensure_image: Mock,
        mock_ensure_container: Mock,
        mock_wait_ready: Mock,
    ) -> None:
        mock_client = Mock()
        mock_ensure_image.return_value = "auto-tester-env:latest"

        mock_container_info = ContainerInfo(
            container_id="container123",
            name="auto-tester-env-test",
            ports=ContainerPorts(vnc=5900),
            status="running",
        )
        mock_ensure_container.return_value = mock_container_info

        mock_health = HealthCheckResult(vnc=True)
        mock_wait_ready.return_value = mock_health

        result = provision_environment(
            mock_client, name_suffix="test", api_key="test-key"
        )

        self.assertEqual(result, mock_container_info)
        mock_ensure_image.assert_called_once_with(mock_client)
        mock_ensure_container.assert_called_once_with(
            mock_client, name_suffix="test", api_key="test-key"
        )
        mock_wait_ready.assert_called_once_with(mock_container_info.ports)

    @patch("environments.services.orchestration.remove_container")
    def test_tears_down_environment_by_removing_container(
        self, mock_remove: Mock
    ) -> None:
        mock_client = Mock()
        teardown_environment(mock_client, "container123")
        mock_remove.assert_called_once_with(mock_client, "container123", force=True)

    @patch("environments.services.orchestration.remove_container")
    def test_tears_down_environment_idempotently(self, mock_remove: Mock) -> None:
        mock_client = Mock()

        teardown_environment(mock_client, "nonexistent")

        mock_remove.assert_called_once_with(mock_client, "nonexistent", force=True)

    @patch("environments.services.orchestration.check_vnc_connection")
    def test_verifies_vnc_service_successfully(self, mock_check_vnc: Mock) -> None:
        mock_check_vnc.return_value = True

        ports = ContainerPorts(vnc=5900)
        result = verify_vnc_service(ports)

        self.assertTrue(result)
        mock_check_vnc.assert_called_once_with(ports)

    @patch("environments.services.orchestration.verify_vnc_service")
    def test_runs_full_verification_successfully(
        self,
        mock_verify_vnc: Mock,
    ) -> None:
        mock_verify_vnc.return_value = True

        container_info = ContainerInfo(
            container_id="container123",
            name="auto-tester-env-test",
            ports=ContainerPorts(vnc=5900),
            status="running",
        )

        result = full_verification(container_info)

        self.assertIsInstance(result, HealthCheckResult)
        self.assertTrue(result.vnc)
        self.assertTrue(result.is_healthy)
        mock_verify_vnc.assert_called_once_with(container_info.ports)


class TypeTests(TestCase):

    def test_creates_container_ports_with_all_fields(self) -> None:
        ports = ContainerPorts(vnc=5900)
        self.assertEqual(ports.vnc, 5900)

    def test_creates_container_info_with_all_fields(self) -> None:
        ports = ContainerPorts(vnc=5900)
        info = ContainerInfo(
            container_id="abc123",
            name="test-container",
            ports=ports,
            status="running",
        )
        self.assertEqual(info.container_id, "abc123")
        self.assertEqual(info.name, "test-container")
        self.assertEqual(info.ports, ports)
        self.assertEqual(info.status, "running")

    def test_is_healthy_when_vnc_passes(self) -> None:
        result = HealthCheckResult(vnc=True)
        self.assertTrue(result.is_healthy)

    def test_is_not_healthy_when_vnc_fails(self) -> None:
        result = HealthCheckResult(vnc=False)
        self.assertFalse(result.is_healthy)
