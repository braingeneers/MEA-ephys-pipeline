"""Small offline metadata validator; scientific stage commands own their own CLIs."""

import argparse
from pathlib import Path
import sys

from .contracts import (ContractError, decode_json, validate_envelope, validate_stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--transport", choices=("s3", "local"), default="s3")
    args = parser.parse_args()
    try:
        with args.document.open("rb") as stream:
            value = decode_json(stream.read(32 * 1024 * 1024 + 1))
        if not isinstance(value, dict):
            raise ContractError("Expected an artifact document")
        if value.get("document_type") == "envelope":
            validate_envelope(value, transport=args.transport)
        else:
            validate_stage(value, transport=args.transport)
    except (ContractError, OSError) as error:
        print(type(error).__name__ + ": metadata validation failed", file=sys.stderr)
        return 2
    print("Valid " + value["contract"] + " " + value["document_type"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
