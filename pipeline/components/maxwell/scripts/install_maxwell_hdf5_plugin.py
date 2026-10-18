#!/usr/bin/env python3
"""Install the checksum-pinned Maxwell HDF5 plugin while building the image."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)


PLUGIN_URL = (
    "https://share.mxwbio.com/d/7f2d1e98a1724a1b8b35/files/"
    "?p=%2FLinux%2Flibcompression.so&dl=1"
)
PLUGIN_SHA256 = "4bc74ea98c08b70406aa976950fcf5f55d572430f51d3882eeb947c557dd14f4"
LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


def download_once(destination: Path) -> None:
    """Download, fully consume, verify, and atomically install one plugin copy."""
    with urlopen(PLUGIN_URL, timeout=60) as response:
        payload = response.read()

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != PLUGIN_SHA256:
        raise ValueError(
            "Maxwell HDF5 plugin checksum mismatch: "
            f"expected={PLUGIN_SHA256}, actual={actual_sha256}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, destination)


@retry(
    retry=retry_if_exception_type((OSError, TimeoutError, URLError, ValueError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30) + wait_random(0, 1),
    before_sleep=before_sleep_log(LOGGER, logging.WARNING),
    reraise=True,
)
def install_with_retry(destination: Path) -> None:
    download_once(destination)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    install_with_retry(args.destination)
    print(
        "Installed checksum-pinned Maxwell HDF5 plugin: "
        f"{args.destination} sha256={PLUGIN_SHA256}"
    )


if __name__ == "__main__":
    main()
