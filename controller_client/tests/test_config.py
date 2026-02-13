from unittest.mock import MagicMock, patch

from controller_client.config import ClientConfig, load_config


class TestClientConfig:
    def test_ws_url_default_port(self) -> None:
        config = ClientConfig(
            host="example.com",
            port=8000,
            api_key="key",
            reconnect_interval=5,
            max_reconnect_attempts=10,
            log_level="INFO",
        )
        assert config.ws_url == "ws://example.com:8000/ws/controller/"

    def test_ws_url_ssl_port(self) -> None:
        config = ClientConfig(
            host="example.com",
            port=443,
            api_key="key",
            reconnect_interval=5,
            max_reconnect_attempts=10,
            log_level="INFO",
        )
        assert config.ws_url == "wss://example.com:443/ws/controller/"

    def test_frozen(self) -> None:
        config = ClientConfig(
            host="h",
            port=1,
            api_key="k",
            reconnect_interval=1,
            max_reconnect_attempts=1,
            log_level="INFO",
        )
        try:
            config.host = "other"  # type: ignore[misc]
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestLoadConfig:
    @patch("controller_client.config.decouple_config")
    def test_cli_args_override_env(self, mock_decouple: MagicMock) -> None:
        config = load_config(
            [
                "--host",
                "myhost",
                "--port",
                "9090",
                "--api-key",
                "mykey",
                "--reconnect-interval",
                "3",
                "--max-reconnect-attempts",
                "5",
                "--log-level",
                "DEBUG",
            ]
        )
        assert config.host == "myhost"
        assert config.port == 9090
        assert config.api_key == "mykey"
        assert config.reconnect_interval == 3
        assert config.max_reconnect_attempts == 5
        assert config.log_level == "DEBUG"

    @patch("controller_client.config.decouple_config")
    def test_env_fallback(self, mock_decouple: MagicMock) -> None:
        mock_decouple.side_effect = lambda key, default="": {
            "CONTROLLER_HOST": "envhost",
            "CONTROLLER_PORT": "7070",
            "CONTROLLER_API_KEY": "envkey",
            "CONTROLLER_RECONNECT_INTERVAL": "7",
            "CONTROLLER_MAX_RECONNECT_ATTEMPTS": "15",
            "CONTROLLER_LOG_LEVEL": "WARNING",
        }.get(key, default)

        config = load_config([])
        assert config.host == "envhost"
        assert config.port == 7070
        assert config.api_key == "envkey"
        assert config.reconnect_interval == 7
        assert config.max_reconnect_attempts == 15
        assert config.log_level == "WARNING"
