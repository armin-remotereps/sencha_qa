"""Grab one frame from a Mutter ScreenCast PipeWire node into a PIL image.

The GNOME backend links a ScreenCast session to its RemoteDesktop session and
records the primary monitor; Mutter then announces the stream's PipeWire node
id (``PipeWireStreamAdded``). This module turns that node into a screenshot by
running the ``gst-launch-1.0`` CLI once per shot, so the runner needs neither
PyGObject nor GStreamer Python bindings. The pattern mirrors the macOS
``screencapture`` path in ``screen_capture.py``: write into a temporary
directory, check the exit status and the file, then decode and detach.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from PIL import Image

from controller_client.exceptions import ScreenCaptureError

_GST_LAUNCH: Final = "gst-launch-1.0"
_GST_INSPECT: Final = "gst-inspect-1.0"
_PIPEWIRE_ELEMENT: Final = "pipewiresrc"
_INSPECT_TIMEOUT_S: Final = 10.0

# The pipewiresrc property that takes Mutter's PipeWire *node id*.
# ``path`` is the legacy property: pipewiresrc parses it as an integer and
# passes it as the stream's target id. Newer PipeWire prefers
# ``target-object``, but that expects an object *serial* or node *name*, and
# Mutter only gives us the node id, so ``target-object=<node id>`` does not
# reliably match. gnome-shell's own screencast service still builds
# ``pipewiresrc path=<node id>`` for exactly this stream. Must be confirmed on
# Ubuntu 26.04 with ``gst-inspect-1.0 pipewiresrc`` (the property is
# deprecated, not removed, as of PipeWire 1.x).
#
# No ``always-copy`` or ``keepalive-time``: gnome-shell sets ``always-copy``
# only so a long recording does not starve Mutter of buffers while a slow
# encoder holds them, which cannot happen with ``num-buffers=1``. Mutter
# records a frame as soon as a consumer connects, so a one-shot grab of a
# static screen still gets its buffer without a keepalive resend.
PIPEWIRE_TARGET_PROPERTY: Final = "path"

GSTREAMER_PACKAGES: Final = (
    "gstreamer1.0-tools gstreamer1.0-pipewire "
    "gstreamer1.0-plugins-base gstreamer1.0-plugins-good"
)
INSTALL_HINT: Final = (
    f"Install them with `sudo apt install {GSTREAMER_PACKAGES}`, or re-run "
    "controller_client/scripts/setup.sh."
)


def grab_frame(node_id: int, timeout_s: float) -> Image.Image:
    """Return one frame of PipeWire node ``node_id``, or raise ScreenCaptureError."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "frame.png"
        completed = _run_gst_launch(node_id, target, timeout_s)

        if completed.returncode != 0:
            raise ScreenCaptureError(_failure_message(node_id, completed))
        if not _has_image_bytes(target):
            raise ScreenCaptureError(
                f"GStreamer finished without writing a frame from PipeWire node "
                f"{node_id}. The screen-cast stream may have closed (screen "
                "locked, or the GNOME session ended); unlock the screen and "
                "retry."
            )

        try:
            with Image.open(target) as image:
                image.load()
                # Detach from the temp file before the directory is removed.
                captured: Image.Image = image.copy()
                return captured
        except OSError as exc:
            raise ScreenCaptureError(
                f"GStreamer wrote a frame that could not be decoded ({exc}). "
                "Retry; if it keeps failing, check the GStreamer PNG plugin "
                f"(gstreamer1.0-plugins-good). {INSTALL_HINT}"
            ) from exc


def _run_gst_launch(
    node_id: int, target: Path, timeout_s: float
) -> subprocess.CompletedProcess[str]:
    args = [
        _GST_LAUNCH,
        "-q",
        _PIPEWIRE_ELEMENT,
        f"{PIPEWIRE_TARGET_PROPERTY}={node_id}",
        "num-buffers=1",
        "!",
        "videoconvert",
        "!",
        "pngenc",
        "!",
        "filesink",
        f"location={target}",
    ]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise ScreenCaptureError(
            f"No frame arrived from PipeWire node {node_id} within {timeout_s}s. "
            "The screen-cast stream may be stalled or the screen locked; unlock "
            "the screen and retry."
        ) from exc
    except OSError as exc:
        raise ScreenCaptureError(
            f"Could not run {_GST_LAUNCH} ({exc}). The GStreamer PipeWire "
            f"tools are missing. {INSTALL_HINT}"
        ) from exc


def _has_image_bytes(target: Path) -> bool:
    return target.exists() and target.stat().st_size > 0


def _failure_message(node_id: int, completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr.strip().splitlines()
    reason = stderr[0] if stderr else f"{_GST_LAUNCH} produced no frame"
    return (
        f"Capturing PipeWire node {node_id} failed: {reason} (exit code "
        f"{completed.returncode}). If the element '{_PIPEWIRE_ELEMENT}' is "
        f"missing, {INSTALL_HINT} Otherwise the screen-cast stream closed; "
        "unlock the screen and retry."
    )


def missing_capture_tools() -> str | None:
    """Return what GStreamer capture tooling is missing, or None if it is all there."""
    if shutil.which(_GST_LAUNCH) is None:
        return (
            f"{_GST_LAUNCH} is not on PATH (package gstreamer1.0-tools). "
            f"{INSTALL_HINT}"
        )
    try:
        completed = subprocess.run(
            [_GST_INSPECT, _PIPEWIRE_ELEMENT],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return (
            f"`{_GST_INSPECT} {_PIPEWIRE_ELEMENT}` did not finish within "
            f"{_INSPECT_TIMEOUT_S}s; the GStreamer registry may be broken. "
            f"{INSTALL_HINT}"
        )
    except OSError as exc:
        return (
            f"Could not run {_GST_INSPECT} ({exc}) (package "
            f"gstreamer1.0-tools). {INSTALL_HINT}"
        )
    if completed.returncode != 0:
        return (
            f"The GStreamer element '{_PIPEWIRE_ELEMENT}' is not available "
            f"(package gstreamer1.0-pipewire). {INSTALL_HINT}"
        )
    return None
