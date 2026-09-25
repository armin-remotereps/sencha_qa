"""Locating, repairing and validating the on-disk OmniParser weights layout.

The Hugging Face repo ships the caption model in ``icon_caption``, but the
vendored OmniParser loader expects ``icon_caption_florence``, so the folder has
to be renamed after every download. Doing that rename in shell (once per setup
script, three dialects) only worked when the destination did not exist yet: an
interrupted download that left a checkpoint-less ``icon_caption_florence``
behind made every later download land in ``icon_caption`` and stay there, and
the failure only surfaced much later as a transformers "no file named
model.safetensors" error at model-load time.

This module is the single implementation of that layout: the setup scripts run
it after downloading, and the executor runs it again before loading, so an
installation that is already in the broken state repairs itself.

Standalone by design. The setup scripts run it as a plain file, from a working
directory where the ``controller_client`` package is not importable, so it must
not import anything else from the package.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DETECTOR_DIR_NAME: Final[str] = "icon_detect"
DETECTOR_FILE_NAME: Final[str] = "model.pt"
CAPTION_DIR_NAME: Final[str] = "icon_caption_florence"
DOWNLOADED_CAPTION_DIR_NAME: Final[str] = "icon_caption"

# Every checkpoint filename ``transformers.from_pretrained`` accepts in a local
# directory, including the index files of a sharded checkpoint. Presence of one
# of these is what separates a usable caption folder from one holding nothing
# but config.json and generation_config.json.
CAPTION_CHECKPOINT_FILE_NAMES: Final[tuple[str, ...]] = (
    "model.safetensors",
    "model.safetensors.index.json",
    "pytorch_model.bin",
    "pytorch_model.bin.index.json",
    "tf_model.h5",
    "model.ckpt.index",
    "flax_model.msgpack",
)

DOWNLOAD_HINT: Final[str] = (
    "Re-run controller_client/scripts/download_omniparser_weights.sh (the "
    "setup scripts run it for you) to download microsoft/OmniParser-v2.0 "
    "again, or point OMNIPARSER_WEIGHTS_DIR at a complete weights folder."
)


@dataclass(frozen=True)
class OmniParserWeights:
    """A weights folder that has been verified to hold every required file."""

    weights_dir: str
    som_model_path: Path
    caption_model_path: Path
    caption_checkpoint_path: Path


def detector_path(weights_dir: str | Path) -> Path:
    return Path(weights_dir) / DETECTOR_DIR_NAME / DETECTOR_FILE_NAME


def caption_dir(weights_dir: str | Path) -> Path:
    return Path(weights_dir) / CAPTION_DIR_NAME


def downloaded_caption_dir(weights_dir: str | Path) -> Path:
    return Path(weights_dir) / DOWNLOADED_CAPTION_DIR_NAME


def find_caption_checkpoint(directory: Path) -> Path | None:
    """Return the model checkpoint inside a caption folder, if it holds one."""
    for name in CAPTION_CHECKPOINT_FILE_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def normalize_caption_weights_layout(weights_dir: str | Path) -> str:
    """Move a freshly downloaded caption model to the name the loader reads.

    Idempotent and safe to call on a healthy install. Returns a one-line
    description of what happened, for the setup scripts to print.
    """
    canonical = caption_dir(weights_dir)
    downloaded = downloaded_caption_dir(weights_dir)

    if find_caption_checkpoint(canonical) is not None:
        return f"Caption weights already in place at {canonical}."
    if find_caption_checkpoint(downloaded) is None:
        return f"No downloaded caption weights to move into {canonical}."

    canonical.mkdir(parents=True, exist_ok=True)
    moved = _merge_directory(downloaded, canonical)
    return f"Moved {moved} caption weight files from {downloaded} to {canonical}."


def _merge_directory(source: Path, target: Path) -> int:
    """Move every entry of source into target, replacing same-named files.

    Replacing rather than deleting the destination up front: the folder left
    behind by an interrupted download holds the same config files as the
    complete one, so overwriting them loses nothing, while an unconditional
    delete would throw away whatever else a user had put there.
    """
    moved = 0
    for entry in sorted(source.iterdir()):
        destination = target / entry.name
        if destination.exists():
            if destination.is_dir():
                _merge_directory(entry, destination)
                continue
            destination.unlink()
        entry.replace(destination)
        moved += 1
    source.rmdir()
    return moved


def validate_weights_layout(weights_dir: str | Path) -> OmniParserWeights:
    """Verify every required weights file, failing loudly on a partial download.

    A missing weights path is not just a normal file-not-found: the vendored
    YOLO loader treats an unrecognized local path as a Hugging Face Hub model
    ID and silently starts downloading it instead of raising, and transformers
    reports a checkpoint-less caption folder as an opaque "no file named
    model.safetensors" error.
    """
    som_model_path = detector_path(weights_dir)
    caption_model_path = caption_dir(weights_dir)
    if not som_model_path.is_file():
        raise FileNotFoundError(
            f"OmniParser detector weights not found at {som_model_path}. "
            f"{DOWNLOAD_HINT}"
        )
    if not caption_model_path.is_dir():
        raise FileNotFoundError(
            f"OmniParser caption weights not found at {caption_model_path}. "
            f"{DOWNLOAD_HINT}"
        )
    caption_checkpoint_path = find_caption_checkpoint(caption_model_path)
    if caption_checkpoint_path is None:
        raise FileNotFoundError(
            f"OmniParser caption weights at {caption_model_path} hold no model "
            f"checkpoint (expected one of "
            f"{', '.join(CAPTION_CHECKPOINT_FILE_NAMES)}), so the download is "
            f"incomplete. {DOWNLOAD_HINT}"
        )
    return OmniParserWeights(
        weights_dir=str(weights_dir),
        som_model_path=som_model_path,
        caption_model_path=caption_model_path,
        caption_checkpoint_path=caption_checkpoint_path,
    )


def resolve_omniparser_weights(weights_dir: str | Path) -> OmniParserWeights:
    """Repair the caption folder name if needed, then validate the layout."""
    normalize_caption_weights_layout(weights_dir)
    return validate_weights_layout(weights_dir)


def main(argv: list[str]) -> int:
    """Entry point for the setup scripts: normalize, then verify, then report."""
    if len(argv) != 1:
        print("usage: omniparser_weights.py <weights-dir>", file=sys.stderr)
        return 2
    weights_dir = argv[0]
    print(normalize_caption_weights_layout(weights_dir))
    try:
        weights = validate_weights_layout(weights_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(
        f"Weights verified: {weights.som_model_path} and "
        f"{weights.caption_checkpoint_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
