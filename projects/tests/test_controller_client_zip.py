from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase

from projects.services import _should_include_path


class ShouldIncludePathTests(SimpleTestCase):
    def test_includes_ordinary_source_file(self) -> None:
        self.assertTrue(_should_include_path(Path("client.py")))
        self.assertTrue(_should_include_path(Path("omniparser/util/omniparser.py")))

    def test_excludes_venv(self) -> None:
        self.assertFalse(_should_include_path(Path(".venv/bin/python")))

    def test_excludes_downloaded_omniparser_weights(self) -> None:
        self.assertFalse(
            _should_include_path(Path("omniparser/weights/icon_detect/model.pt"))
        )

    def test_excludes_pycache_and_tool_caches(self) -> None:
        self.assertFalse(
            _should_include_path(Path("__pycache__/client.cpython-313.pyc"))
        )
        self.assertFalse(_should_include_path(Path(".pytest_cache/CACHEDIR.TAG")))
        self.assertFalse(
            _should_include_path(Path(".mypy_cache/3.13/client.data.json"))
        )

    def test_excludes_tests_directory(self) -> None:
        self.assertFalse(_should_include_path(Path("tests/test_protocol.py")))

    def test_excludes_env_file(self) -> None:
        self.assertFalse(_should_include_path(Path(".env")))
