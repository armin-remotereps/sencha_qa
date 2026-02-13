from unittest.mock import MagicMock, patch

from controller_client.system_info import (
    SystemInfo,
    _normalize_architecture,
    gather_system_info,
)


class TestNormalizeArchitecture:
    def test_x86_64(self) -> None:
        assert _normalize_architecture("x86_64") == "AMD64"

    def test_amd64(self) -> None:
        assert _normalize_architecture("amd64") == "AMD64"

    def test_aarch64(self) -> None:
        assert _normalize_architecture("aarch64") == "ARM64"

    def test_arm64(self) -> None:
        assert _normalize_architecture("arm64") == "ARM64"

    def test_unknown_passthrough(self) -> None:
        assert _normalize_architecture("riscv64") == "riscv64"

    def test_case_insensitive(self) -> None:
        assert _normalize_architecture("X86_64") == "AMD64"
        assert _normalize_architecture("AARCH64") == "ARM64"


class TestSystemInfoToDict:
    def test_to_dict(self) -> None:
        info = SystemInfo(
            os="Linux",
            os_version="6.1.0",
            architecture="AMD64",
            hostname="test-host",
            screen_width=1920,
            screen_height=1080,
        )
        d = info.to_dict()
        assert d["os"] == "Linux"
        assert d["os_version"] == "6.1.0"
        assert d["architecture"] == "AMD64"
        assert d["hostname"] == "test-host"
        assert d["screen_width"] == 1920
        assert d["screen_height"] == 1080


class TestGatherSystemInfo:
    @patch("controller_client.system_info.socket")
    @patch("controller_client.system_info.platform")
    @patch("controller_client.system_info._get_screen_resolution")
    def test_gather(
        self,
        mock_resolution: MagicMock,
        mock_platform: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        mock_platform.system.return_value = "Linux"
        mock_platform.version.return_value = "6.1.0"
        mock_platform.machine.return_value = "x86_64"
        mock_socket.gethostname.return_value = "my-host"
        mock_resolution.return_value = (2560, 1440)

        info = gather_system_info()
        assert info.os == "Linux"
        assert info.os_version == "6.1.0"
        assert info.architecture == "AMD64"
        assert info.hostname == "my-host"
        assert info.screen_width == 2560
        assert info.screen_height == 1440

    @patch("controller_client.system_info.socket")
    @patch("controller_client.system_info.platform")
    @patch("controller_client.system_info._get_screen_resolution")
    def test_gather_arm(
        self,
        mock_resolution: MagicMock,
        mock_platform: MagicMock,
        mock_socket: MagicMock,
    ) -> None:
        mock_platform.system.return_value = "Darwin"
        mock_platform.version.return_value = "23.0.0"
        mock_platform.machine.return_value = "aarch64"
        mock_socket.gethostname.return_value = "mac-host"
        mock_resolution.return_value = (3024, 1964)

        info = gather_system_info()
        assert info.os == "Darwin"
        assert info.architecture == "ARM64"
