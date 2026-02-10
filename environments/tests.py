from __future__ import annotations

from unittest.mock import MagicMock, Mock, call, patch

from django.test import TestCase

from environments.services import (
    build_environment_image,
    check_container_health,
    check_vnc_connection,
    close_docker_client,
    close_ssh_connection,
    create_container,
    create_ssh_connection,
    ensure_environment_image,
    execute_ssh_command,
    full_verification,
    get_container_info,
    get_docker_client,
    get_playwright_cdp_url,
    image_exists,
    list_environment_containers,
    provision_environment,
    remove_container,
    teardown_environment,
    verify_playwright_connection,
    verify_playwright_service,
    verify_ssh_service,
    verify_vnc_service,
    wait_for_container_ready,
)
from environments.types import (
    ContainerInfo,
    ContainerPorts,
    HealthCheckResult,
    SSHResult,
    VerificationResult,
)

# ============================================================================
# DOCKER CLIENT TESTS
# ============================================================================


class DockerClientTests(TestCase):

    @patch("environments.services.docker.DockerClient")
    def test_should_create_docker_client_with_host_from_settings(
        self, mock_docker_client: Mock
    ) -> None:
        mock_client = Mock()
        mock_docker_client.return_value = mock_client

        result = get_docker_client()

        self.assertEqual(result, mock_client)
        mock_docker_client.assert_called_once_with(
            base_url="unix:///var/run/docker.sock"
        )

    def test_should_close_docker_client_connection(self) -> None:
        mock_client = Mock()
        close_docker_client(mock_client)
        mock_client.close.assert_called_once()


# ============================================================================
# IMAGE MANAGEMENT TESTS
# ============================================================================


class ImageManagementTests(TestCase):

    def test_should_return_true_when_image_exists(self) -> None:
        mock_client = Mock()
        mock_client.images.get.return_value = Mock()

        result = image_exists(mock_client)

        self.assertTrue(result)
        mock_client.images.get.assert_called_once_with("auto-tester-env:latest")

    def test_should_return_false_when_image_not_found(self) -> None:
        mock_client = Mock()
        import docker.errors

        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        result = image_exists(mock_client)

        self.assertFalse(result)

    @patch("environments.services.Path")
    def test_should_build_environment_image_with_correct_parameters(
        self, mock_path: Mock
    ) -> None:
        mock_client = Mock()
        mock_dockerfile_path = Mock()
        mock_path.return_value.resolve.return_value.parent = Mock()
        mock_path.return_value.resolve.return_value.parent.__truediv__ = Mock(
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
            {"SSH_PASSWORD": "testpass123", "VNC_PASSWORD": "testpass123"},
        )
        self.assertTrue(call_kwargs["nocache"])
        self.assertTrue(call_kwargs["rm"])

    def test_should_build_image_when_not_exists(self) -> None:
        mock_client = Mock()
        import docker.errors

        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        with patch("environments.services.build_environment_image") as mock_build:
            mock_build.return_value = "auto-tester-env:latest"
            result = ensure_environment_image(mock_client)

        self.assertEqual(result, "auto-tester-env:latest")
        mock_build.assert_called_once_with(mock_client)

    def test_should_not_build_image_when_exists(self) -> None:
        mock_client = Mock()
        mock_client.images.get.return_value = Mock()

        with patch("environments.services.build_environment_image") as mock_build:
            result = ensure_environment_image(mock_client)

        self.assertEqual(result, "auto-tester-env:latest")
        mock_build.assert_not_called()


# ============================================================================
# CONTAINER LIFECYCLE TESTS
# ============================================================================


class ContainerLifecycleTests(TestCase):

    def test_should_create_container_with_auto_generated_suffix(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_container.id = "container123"
        mock_container.name = "auto-tester-env-abc12345"
        mock_container.status = "running"
        mock_container.ports = {
            "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32768"}],
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32769"}],
            "9223/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32770"}],
        }

        mock_client.containers.create.return_value = mock_container

        result = create_container(mock_client)

        self.assertIsInstance(result, ContainerInfo)
        self.assertEqual(result.container_id, "container123")
        self.assertEqual(result.name, "auto-tester-env-abc12345")
        self.assertEqual(result.ports.ssh, 32768)
        self.assertEqual(result.ports.vnc, 32769)
        self.assertEqual(result.ports.playwright_cdp, 32770)
        mock_container.start.assert_called_once()
        mock_container.reload.assert_called_once()

    def test_should_create_container_with_custom_suffix(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_container.id = "container456"
        mock_container.name = "auto-tester-env-custom"
        mock_container.status = "running"
        mock_container.ports = {
            "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40000"}],
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40001"}],
            "9223/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40002"}],
        }

        mock_client.containers.create.return_value = mock_container

        result = create_container(mock_client, name_suffix="custom")

        self.assertEqual(result.name, "auto-tester-env-custom")
        mock_client.containers.create.assert_called_once()
        call_kwargs = mock_client.containers.create.call_args[1]
        self.assertEqual(call_kwargs["name"], "auto-tester-env-custom")

    def test_should_get_container_info_with_fresh_state(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_container.id = "container789"
        mock_container.name = "auto-tester-env-test"
        mock_container.status = "running"
        mock_container.ports = {
            "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "50000"}],
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "50001"}],
            "9223/tcp": [{"HostIp": "0.0.0.0", "HostPort": "50002"}],
        }

        mock_client.containers.get.return_value = mock_container

        result = get_container_info(mock_client, "container789")

        self.assertEqual(result.container_id, "container789")
        self.assertEqual(result.ports.ssh, 50000)
        mock_container.reload.assert_called_once()

    def test_should_remove_container_with_force(self) -> None:
        mock_client = Mock()
        mock_container = Mock()
        mock_client.containers.get.return_value = mock_container

        remove_container(mock_client, "container123", force=True)

        mock_container.remove.assert_called_once_with(force=True)

    def test_should_ignore_not_found_error_when_removing_container(self) -> None:
        mock_client = Mock()
        import docker.errors

        mock_client.containers.get.side_effect = docker.errors.NotFound("not found")

        remove_container(mock_client, "nonexistent")

    def test_should_list_all_environment_containers(self) -> None:
        mock_client = Mock()
        mock_container1 = Mock()
        mock_container1.id = "container1"
        mock_container1.name = "auto-tester-env-one"
        mock_container1.status = "running"
        mock_container1.ports = {
            "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10000"}],
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10001"}],
            "9223/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10002"}],
        }

        mock_container2 = Mock()
        mock_container2.id = "container2"
        mock_container2.name = "auto-tester-env-two"
        mock_container2.status = "exited"
        mock_container2.ports = {
            "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20000"}],
            "5900/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20001"}],
            "9223/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20002"}],
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


# ============================================================================
# HEALTH CHECK TESTS
# ============================================================================


class HealthCheckTests(TestCase):

    @patch("environments.services._check_port")
    def test_should_return_all_ports_ok_when_healthy(
        self, mock_check_port: Mock
    ) -> None:
        mock_check_port.return_value = True
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)

        result = check_container_health(ports)

        self.assertTrue(result.ssh_ok)
        self.assertTrue(result.vnc_ok)
        self.assertTrue(result.playwright_ok)
        self.assertTrue(result.all_ok)
        self.assertEqual(mock_check_port.call_count, 3)

    @patch("environments.services._check_port")
    def test_should_return_failed_ports_when_not_healthy(
        self, mock_check_port: Mock
    ) -> None:
        mock_check_port.side_effect = [True, False, True]
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)

        result = check_container_health(ports)

        self.assertTrue(result.ssh_ok)
        self.assertFalse(result.vnc_ok)
        self.assertTrue(result.playwright_ok)
        self.assertFalse(result.all_ok)

    @patch("environments.services.check_container_health")
    @patch("environments.services.time")
    def test_should_return_when_container_becomes_ready(
        self, mock_time: Mock, mock_check_health: Mock
    ) -> None:
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)

        mock_time.monotonic.side_effect = [0.0, 1.0, 2.0]
        mock_time.sleep = Mock()

        unhealthy = HealthCheckResult(ssh_ok=False, vnc_ok=False, playwright_ok=False)
        healthy = HealthCheckResult(ssh_ok=True, vnc_ok=True, playwright_ok=True)
        mock_check_health.side_effect = [unhealthy, healthy]

        result = wait_for_container_ready(ports, timeout=10, interval=1)

        self.assertTrue(result.all_ok)
        self.assertEqual(mock_check_health.call_count, 2)

    @patch("environments.services.check_container_health")
    @patch("environments.services.time")
    def test_should_raise_timeout_error_when_not_ready(
        self, mock_time: Mock, mock_check_health: Mock
    ) -> None:
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)

        mock_time.monotonic.side_effect = [0.0, 1.0, 2.0, 3.0]
        mock_time.sleep = Mock()

        unhealthy = HealthCheckResult(ssh_ok=False, vnc_ok=False, playwright_ok=False)
        mock_check_health.return_value = unhealthy

        with self.assertRaises(TimeoutError) as context:
            wait_for_container_ready(ports, timeout=2, interval=1)

        self.assertIn("not ready after 2 seconds", str(context.exception))


# ============================================================================
# SSH TESTS
# ============================================================================


class SSHTests(TestCase):

    @patch("environments.services.paramiko.SSHClient")
    def test_should_create_ssh_connection_with_correct_parameters(
        self, mock_ssh_client_class: Mock
    ) -> None:
        mock_ssh_client = Mock()
        mock_ssh_client_class.return_value = mock_ssh_client

        ports = ContainerPorts(ssh=32768, vnc=5900, playwright_cdp=9222)
        result = create_ssh_connection(ports)

        self.assertEqual(result, mock_ssh_client)
        mock_ssh_client.set_missing_host_key_policy.assert_called_once()
        mock_ssh_client.connect.assert_called_once_with(
            hostname="localhost",
            port=32768,
            username="root",
            password="testpass123",
            timeout=10,
        )

    def test_should_execute_ssh_command_and_return_result(self) -> None:
        mock_ssh_client = Mock()
        mock_stdin = Mock()
        mock_stdout = Mock()
        mock_stderr = Mock()

        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stdout.read.return_value = b"hello world\n"
        mock_stderr.read.return_value = b""

        mock_ssh_client.exec_command.return_value = (
            mock_stdin,
            mock_stdout,
            mock_stderr,
        )

        result = execute_ssh_command(mock_ssh_client, "echo hello")

        self.assertIsInstance(result, SSHResult)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "hello world\n")
        self.assertEqual(result.stderr, "")

    def test_should_close_ssh_connection(self) -> None:
        mock_ssh_client = Mock()
        close_ssh_connection(mock_ssh_client)
        mock_ssh_client.close.assert_called_once()


# ============================================================================
# VNC TESTS
# ============================================================================


class VNCTests(TestCase):

    @patch("environments.services.socket.create_connection")
    def test_should_return_true_when_rfb_protocol_detected(
        self, mock_create_connection: Mock
    ) -> None:
        mock_socket = MagicMock()
        mock_socket.recv.return_value = b"RFB"
        mock_socket.__enter__.return_value = mock_socket
        mock_socket.__exit__.return_value = None
        mock_create_connection.return_value = mock_socket

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = check_vnc_connection(ports)

        self.assertTrue(result)

    @patch("environments.services.socket.create_connection")
    def test_should_return_false_when_connection_fails(
        self, mock_create_connection: Mock
    ) -> None:
        import socket

        mock_create_connection.side_effect = socket.error("Connection refused")

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = check_vnc_connection(ports)

        self.assertFalse(result)


# ============================================================================
# PLAYWRIGHT TESTS
# ============================================================================


class PlaywrightTests(TestCase):

    def test_should_return_correct_cdp_url(self) -> None:
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = get_playwright_cdp_url(ports)
        self.assertEqual(result, "http://localhost:9222")

    @patch("environments.services.sync_playwright")
    def test_should_return_true_when_playwright_connection_succeeds(
        self, mock_sync_playwright: Mock
    ) -> None:
        mock_playwright = Mock()
        mock_browser = Mock()
        mock_page = Mock()

        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        mock_playwright.chromium.connect_over_cdp.return_value = mock_browser
        mock_browser.new_page.return_value = mock_page

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = verify_playwright_connection(ports)

        self.assertTrue(result)
        mock_browser.new_page.assert_called_once()
        mock_page.goto.assert_called_once_with("about:blank")
        mock_page.close.assert_called_once()
        mock_browser.close.assert_called_once()

    @patch("environments.services.sync_playwright")
    def test_should_return_false_when_playwright_connection_fails(
        self, mock_sync_playwright: Mock
    ) -> None:
        mock_sync_playwright.return_value.__enter__.side_effect = Exception(
            "Connection failed"
        )

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = verify_playwright_connection(ports)

        self.assertFalse(result)


# ============================================================================
# ORCHESTRATION TESTS
# ============================================================================


class OrchestrationTests(TestCase):

    @patch("environments.services.wait_for_container_ready")
    @patch("environments.services.create_container")
    @patch("environments.services.ensure_environment_image")
    def test_should_provision_environment_with_all_steps(
        self,
        mock_ensure_image: Mock,
        mock_create_container: Mock,
        mock_wait_ready: Mock,
    ) -> None:
        mock_client = Mock()
        mock_ensure_image.return_value = "auto-tester-env:latest"

        mock_container_info = ContainerInfo(
            container_id="container123",
            name="auto-tester-env-test",
            ports=ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222),
            status="running",
        )
        mock_create_container.return_value = mock_container_info

        mock_health = HealthCheckResult(ssh_ok=True, vnc_ok=True, playwright_ok=True)
        mock_wait_ready.return_value = mock_health

        result = provision_environment(mock_client, name_suffix="test")

        self.assertEqual(result, mock_container_info)
        mock_ensure_image.assert_called_once_with(mock_client)
        mock_create_container.assert_called_once_with(mock_client, name_suffix="test")
        mock_wait_ready.assert_called_once_with(mock_container_info.ports)

    @patch("environments.services.remove_container")
    def test_should_teardown_environment_by_removing_container(
        self, mock_remove: Mock
    ) -> None:
        mock_client = Mock()
        teardown_environment(mock_client, "container123")
        mock_remove.assert_called_once_with(mock_client, "container123", force=True)

    @patch("environments.services.remove_container")
    def test_should_teardown_environment_idempotently(self, mock_remove: Mock) -> None:
        mock_client = Mock()

        teardown_environment(mock_client, "nonexistent")

        mock_remove.assert_called_once_with(mock_client, "nonexistent", force=True)

    @patch("environments.services.close_ssh_connection")
    @patch("environments.services.execute_ssh_command")
    @patch("environments.services.create_ssh_connection")
    def test_should_verify_ssh_service_successfully(
        self,
        mock_create_ssh: Mock,
        mock_execute_ssh: Mock,
        mock_close_ssh: Mock,
    ) -> None:
        mock_ssh_client = Mock()
        mock_create_ssh.return_value = mock_ssh_client

        mock_ssh_result = SSHResult(exit_code=0, stdout="hello\n", stderr="")
        mock_execute_ssh.return_value = mock_ssh_result

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = verify_ssh_service(ports)

        self.assertTrue(result)
        mock_create_ssh.assert_called_once_with(ports)
        mock_execute_ssh.assert_called_once_with(mock_ssh_client, "echo hello")
        mock_close_ssh.assert_called_once_with(mock_ssh_client)

    @patch("environments.services.create_ssh_connection")
    def test_should_return_false_when_ssh_service_fails(
        self, mock_create_ssh: Mock
    ) -> None:
        mock_create_ssh.side_effect = Exception("Connection failed")

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = verify_ssh_service(ports)

        self.assertFalse(result)

    @patch("environments.services.check_vnc_connection")
    def test_should_verify_vnc_service_successfully(self, mock_check_vnc: Mock) -> None:
        mock_check_vnc.return_value = True

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = verify_vnc_service(ports)

        self.assertTrue(result)
        mock_check_vnc.assert_called_once_with(ports)

    @patch("environments.services.verify_playwright_connection")
    def test_should_verify_playwright_service_successfully(
        self, mock_verify_playwright: Mock
    ) -> None:
        mock_verify_playwright.return_value = True

        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        result = verify_playwright_service(ports)

        self.assertTrue(result)
        mock_verify_playwright.assert_called_once_with(ports)

    @patch("environments.services.verify_playwright_service")
    @patch("environments.services.verify_vnc_service")
    @patch("environments.services.verify_ssh_service")
    def test_should_run_full_verification_successfully(
        self,
        mock_verify_ssh: Mock,
        mock_verify_vnc: Mock,
        mock_verify_playwright: Mock,
    ) -> None:
        mock_verify_ssh.return_value = True
        mock_verify_vnc.return_value = True
        mock_verify_playwright.return_value = True

        container_info = ContainerInfo(
            container_id="container123",
            name="auto-tester-env-test",
            ports=ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222),
            status="running",
        )

        result = full_verification(container_info)

        self.assertIsInstance(result, VerificationResult)
        self.assertTrue(result.ssh)
        self.assertTrue(result.vnc)
        self.assertTrue(result.playwright)
        self.assertTrue(result.all_passed)
        mock_verify_ssh.assert_called_once_with(container_info.ports)
        mock_verify_vnc.assert_called_once_with(container_info.ports)
        mock_verify_playwright.assert_called_once_with(container_info.ports)


# ============================================================================
# TYPE TESTS
# ============================================================================


class TypeTests(TestCase):

    def test_should_create_container_ports_with_all_fields(self) -> None:
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
        self.assertEqual(ports.ssh, 22)
        self.assertEqual(ports.vnc, 5900)
        self.assertEqual(ports.playwright_cdp, 9222)

    def test_should_create_container_info_with_all_fields(self) -> None:
        ports = ContainerPorts(ssh=22, vnc=5900, playwright_cdp=9222)
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

    def test_should_return_true_for_all_ok_when_all_checks_pass(self) -> None:
        result = HealthCheckResult(ssh_ok=True, vnc_ok=True, playwright_ok=True)
        self.assertTrue(result.all_ok)

    def test_should_return_false_for_all_ok_when_any_check_fails(self) -> None:
        result1 = HealthCheckResult(ssh_ok=False, vnc_ok=True, playwright_ok=True)
        result2 = HealthCheckResult(ssh_ok=True, vnc_ok=False, playwright_ok=True)
        result3 = HealthCheckResult(ssh_ok=True, vnc_ok=True, playwright_ok=False)

        self.assertFalse(result1.all_ok)
        self.assertFalse(result2.all_ok)
        self.assertFalse(result3.all_ok)

    def test_should_create_ssh_result_with_all_fields(self) -> None:
        result = SSHResult(exit_code=0, stdout="output", stderr="error")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "output")
        self.assertEqual(result.stderr, "error")

    def test_should_create_verification_result_with_all_fields(self) -> None:
        result = VerificationResult(ssh=True, vnc=True, playwright=True)
        self.assertTrue(result.ssh)
        self.assertTrue(result.vnc)
        self.assertTrue(result.playwright)

    def test_should_return_true_for_all_passed_when_all_verifications_pass(
        self,
    ) -> None:
        result = VerificationResult(ssh=True, vnc=True, playwright=True)
        self.assertTrue(result.all_passed)

    def test_should_return_false_for_all_passed_when_any_verification_fails(
        self,
    ) -> None:
        result1 = VerificationResult(ssh=False, vnc=True, playwright=True)
        result2 = VerificationResult(ssh=True, vnc=False, playwright=True)
        result3 = VerificationResult(ssh=True, vnc=True, playwright=False)

        self.assertFalse(result1.all_passed)
        self.assertFalse(result2.all_passed)
        self.assertFalse(result3.all_passed)
