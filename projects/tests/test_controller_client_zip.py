from __future__ import annotations

import fnmatch
import io
import zipfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from projects.models import Project
from projects.services import _should_include_path, generate_controller_client_zip

_REQUIRED_ARCHIVE_MEMBERS = frozenset(
    {
        "controller_client/__init__.py",
        "controller_client/client.py",
        "controller_client/protocol.py",
        "controller_client/exceptions.py",
        "controller_client/omniparser_executor.py",
        "controller_client/omniparser_config.py",
        "controller_client/omniparser/__init__.py",
        "controller_client/omniparser/util/__init__.py",
        "controller_client/omniparser/util/utils.py",
        "controller_client/omniparser/util/omniparser.py",
        "controller_client/scripts/setup.sh",
        "controller_client/scripts/setup.ps1",
        "controller_client/scripts/setup.bat",
        "controller_client/scripts/download_omniparser_weights.sh",
        "controller_client/requirements.txt",
        "controller_client/example.env",
        "controller_client/.env",
    }
)

# Paths the running server itself depends on (imports + ZIP generation); if
# .dockerignore ever excludes any of them again the production image breaks
# at import time (ModuleNotFoundError: controller_client) or serves an empty
# controller download.
_MUST_REACH_IMAGE = (
    "projects/controller_protocol.py",
    "controller_client/__init__.py",
    "controller_client/protocol.py",
    "controller_client/exceptions.py",
    "controller_client/client.py",
    "controller_client/omniparser/util/utils.py",
    "controller_client/scripts/setup.sh",
    "controller_client/requirements.txt",
)

_MUST_NOT_REACH_IMAGE = (
    "controller_client/.venv/Scripts/python.exe",
    "controller_client/.venv/lib/python3.13/site-packages/torch/__init__.py",
    "controller_client/omniparser/weights/icon_detect/model.pt",
    "controller_client/.env",
)


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


class GenerateControllerClientZipTests(SimpleTestCase):
    @override_settings(
        CONTROLLER_SERVER_HOST="qa.example.test", CONTROLLER_SERVER_PORT=443
    )
    def test_archive_contains_complete_controller_source_and_generated_env(
        self,
    ) -> None:
        project = Project(id=1, name="Zip Project", api_key="zip-test-api-key")

        archive_bytes = generate_controller_client_zip(project)

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = set(archive.namelist())
            env_content = archive.read("controller_client/.env").decode("utf-8")

        missing = _REQUIRED_ARCHIVE_MEMBERS - names
        self.assertEqual(missing, set(), f"controller ZIP is missing {sorted(missing)}")
        self.assertIn("CONTROLLER_API_KEY=zip-test-api-key", env_content)
        self.assertIn("CONTROLLER_HOST=qa.example.test", env_content)
        self.assertIn("CONTROLLER_PORT=443", env_content)

    def test_archive_excludes_venv_weights_caches_and_tests(self) -> None:
        project = Project(id=1, name="Zip Project", api_key="zip-test-api-key")

        archive_bytes = generate_controller_client_zip(project)

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()

        offending = [
            name
            for name in names
            if name.startswith("controller_client/.venv/")
            or "/omniparser/weights/" in name
            or "/__pycache__/" in name
            or "/.pytest_cache/" in name
            or "/.mypy_cache/" in name
            or name.startswith("controller_client/tests/")
        ]
        self.assertEqual(offending, [])
        self.assertEqual(names.count("controller_client/.env"), 1)


def _load_dockerignore_patterns() -> list[str]:
    lines = (settings.BASE_DIR / ".dockerignore").read_text(encoding="utf-8")
    patterns: list[str] = []
    for raw in lines.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.rstrip("/"))
    return patterns


def _dockerignore_matches(pattern: str, path: str) -> bool:
    # Docker excludes a matched directory together with everything beneath it,
    # so a path is ignored if the pattern matches it or any parent directory.
    candidates = [path]
    parts = path.split("/")
    for i in range(1, len(parts)):
        candidates.append("/".join(parts[:i]))
    for candidate in candidates:
        if fnmatch.fnmatchcase(candidate, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(
            candidate.rsplit("/", 1)[-1], pattern[3:]
        ):
            return True
    return False


def _is_dockerignored(path: str, patterns: list[str]) -> bool:
    return any(_dockerignore_matches(pattern, path) for pattern in patterns)


class DockerignoreTests(SimpleTestCase):
    def test_server_side_controller_sources_reach_the_image(self) -> None:
        patterns = _load_dockerignore_patterns()
        ignored = [p for p in _MUST_REACH_IMAGE if _is_dockerignored(p, patterns)]
        self.assertEqual(
            ignored,
            [],
            ".dockerignore excludes files the server imports or packages: "
            f"{ignored}",
        )

    def test_controller_runtime_artifacts_stay_out_of_the_image(self) -> None:
        patterns = _load_dockerignore_patterns()
        leaked = [
            p for p in _MUST_NOT_REACH_IMAGE if not _is_dockerignored(p, patterns)
        ]
        self.assertEqual(
            leaked, [], f".dockerignore lets these into the image: {leaked}"
        )

    def test_required_sources_exist_in_the_checkout(self) -> None:
        missing = [
            p for p in _MUST_REACH_IMAGE if not (settings.BASE_DIR / p).is_file()
        ]
        self.assertEqual(missing, [])
