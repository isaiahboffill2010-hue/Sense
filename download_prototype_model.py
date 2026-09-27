#!/usr/bin/env python3
"""Download and verify the local object detector used by prototype_vision.py."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request


ASSETS = (
    (
        "ssd_mobilenet_v1_coco_quant_postprocess.tflite",
        "https://raw.githubusercontent.com/google-coral/test_data/master/"
        "ssd_mobilenet_v1_coco_quant_postprocess.tflite",
        6_938_269,
        "020d9f0a3e8b84e13cb1dbe70a756ef4dd5884bfc50e77f43c7bac84e791dd9d",
    ),
    (
        "coco_labels.txt",
        "https://raw.githubusercontent.com/google-coral/test_data/master/"
        "coco_labels.txt",
        661,
        "dc183f003fc753c4c43fae6fdf7f387559449573f13fa32e517fb7453fd380f1",
    ),
)


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def download_asset(directory: Path, name: str, url: str, size: int, sha256: str) -> None:
    destination = directory / name
    if destination.is_file() and file_digest(destination) == (size, sha256):
        print(f"Verified existing {destination} ({size:,} bytes)")
        return

    request = urllib.request.Request(url, headers={"User-Agent": "Sense-local-vision/1"})
    temporary_name = None
    try:
        with urllib.request.urlopen(request, timeout=120) as response, tempfile.NamedTemporaryFile(
            prefix=f".{name}.", suffix=".download", dir=directory, delete=False
        ) as temporary:
            temporary_name = temporary.name
            digest = hashlib.sha256()
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                temporary.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)

        actual_hash = digest.hexdigest()
        if downloaded != size or actual_hash != sha256:
            raise RuntimeError(
                f"Integrity check failed for {name}: expected {size} bytes/{sha256}, "
                f"received {downloaded} bytes/{actual_hash}"
            )
        os.replace(temporary_name, destination)
        temporary_name = None
        print(f"Downloaded and verified {destination} ({downloaded:,} bytes)")
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "models",
        help="destination directory (default: ./models)",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for asset in ASSETS:
            download_asset(args.output_dir, *asset)
    except Exception as exc:
        print(f"MODEL DOWNLOAD FAILED: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
