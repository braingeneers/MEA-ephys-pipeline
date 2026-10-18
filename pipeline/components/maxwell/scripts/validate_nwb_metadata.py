#!/usr/bin/env python3
"""Write a machine-readable PyNWB, NWB Inspector, and cell-culture report."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

from nwbinspector import inspect_nwbfile, load_config
from pynwb import NWBHDF5IO, validate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nwb-file", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--metadata-projection", default="")
    return parser.parse_args()


def text(value: Any) -> str:
    return str(getattr(value, "name", value)) if value is not None else ""


def inspector_message(value: Any) -> dict[str, str]:
    fields = (
        "message",
        "importance",
        "severity",
        "check_function_name",
        "object_name",
        "object_type",
        "location",
        "file_path",
    )
    return {
        field: text(getattr(value, field, None))
        for field in fields
        if getattr(value, field, None) is not None
    }


def projection_warnings(path: str) -> list[dict[str, Any]]:
    if not path:
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    warnings = value.get("warnings") or []
    return [warning for warning in warnings if isinstance(warning, dict)]


def main() -> None:
    args = parse_args()
    nwb_path = Path(args.nwb_file)
    if not nwb_path.is_file():
        raise SystemExit(f"NWB file does not exist: {nwb_path}")

    schema_errors = [str(error) for error in validate(path=str(nwb_path))]
    with NWBHDF5IO(str(nwb_path), "r", load_namespaces=True) as io:
        nwbfile = io.read()
        try:
            from ndx_cell_culture import validate_recommended_terms
        except ImportError:
            cell_culture_issues = []
        else:
            cell_culture_issues = [
                str(getattr(issue, "message", issue))
                for issue in validate_recommended_terms(nwbfile)
            ]

    inspector_issues = [
        inspector_message(issue)
        for issue in inspect_nwbfile(
            nwbfile_path=nwb_path,
            config=load_config("dandi"),
        )
    ]
    counts = Counter(issue.get("importance", "UNKNOWN") for issue in inspector_issues)
    report = {
        "report_schema_version": 1,
        "nwb_file": nwb_path.name,
        "schema_valid": not schema_errors,
        "pynwb_schema_errors": schema_errors,
        "nwb_inspector": {
            "issue_count": len(inspector_issues),
            "counts_by_importance": dict(sorted(counts.items())),
            "issues": inspector_issues,
        },
        "ndx_cell_culture_recommendations": cell_culture_issues,
        "uploader_completeness_warnings": projection_warnings(
            args.metadata_projection
        ),
    }
    Path(args.output_file).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if schema_errors:
        raise SystemExit("PyNWB schema validation failed; see validation report.")


if __name__ == "__main__":
    main()
