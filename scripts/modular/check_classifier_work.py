#!/usr/bin/env python3
"""Check classifier-work completeness without running scientific producers."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "pipeline/helpers"))
import portable_bundle as portable
import stage_bundle as stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--positive-bundle", type=Path)
    parser.add_argument("--zero-bundle", type=Path)
    args = parser.parse_args()
    if bool(args.positive_bundle) != bool(args.zero_bundle):
        parser.error("Provide both real positive and zero bundles, or neither")
    args.output.mkdir(parents=True, exist_ok=False)
    header = "decoder_label,decoder_probability\n"
    cases = [
        ("valid_all_analyzer_units", header + "sua,0.95\nmua,0.4\n", 2, None),
        ("valid_zero_with_header", header, 0, None),
        ("valid_probability_bounds", header + "noise,0\nsua,1\n", 2, None),
        ("valid_extra_column", header.rstrip() + ",notes\nsua,1,retained\nmua,0.5,unknown\n", 2, None),
        ("valid_unicode_label", header + "\u03b1,1\n", 1, None),
        ("missing_file", None, 2, "Missing classifier outcome for"),
        ("directory_instead_of_file", "DIRECTORY", 0, "Missing classifier outcome for"),
        ("missing_zero_file", None, 0, "Missing classifier outcome for"),
        ("empty_file", "", 0, "Malformed classifier outcome for"),
        ("wrong_header", "prediction,probability\nsua,1\n", 1, "Malformed classifier outcome for"),
        ("duplicate_header", "decoder_label,decoder_probability,decoder_label\nsua,1,mua\n", 1, "Malformed classifier outcome for"),
        ("missing_unit_row", header + "sua,1\n", 2, "Malformed classifier outcome for"),
        ("extra_unit_row", header + "sua,1\nmua,0.5\n", 1, "Malformed classifier outcome for"),
        ("unexpected_zero_row", header + "sua,1\n", 0, "Malformed classifier outcome for"),
        ("empty_label", header + ",1\n", 1, "Malformed classifier outcome for"),
        ("blank_label", header + "  ,1\n", 1, "Malformed classifier outcome for"),
        ("empty_probability", header + "sua,\n", 1, "Malformed classifier outcome for"),
        ("missing_probability", header + "sua\n", 1, "Malformed classifier outcome for"),
        ("nonnumeric_probability", header + "sua,true\n", 1, "Malformed classifier outcome for"),
        ("nan_probability", header + "sua,nan\n", 1, "Malformed classifier outcome for"),
        ("infinite_probability", header + "sua,inf\n", 1, "Malformed classifier outcome for"),
        ("overflow_probability", header + "sua,1e999\n", 1, "Malformed classifier outcome for"),
        ("negative_probability", header + "sua,-0.1\n", 1, "Malformed classifier outcome for"),
        ("large_probability", header + "sua,1.1\n", 1, "Malformed classifier outcome for"),
        ("unlabelled_extra_field", header + "sua,1,extra\n", 1, "Malformed classifier outcome for"),
        ("malformed_quote", header + 'sua,"1\n', 1, "Malformed classifier outcome for"),
        ("invalid_utf8", b"\xff", 0, "Malformed classifier outcome for"),
    ]
    results = []
    for name, text, total, expected in cases:
        root = args.output / name
        root.mkdir()
        path = root / "unit_classifier_recording.csv"
        if text == "DIRECTORY":
            path.mkdir()
        elif isinstance(text, bytes):
            path.write_bytes(text)
        elif text is not None:
            path.write_text(text, encoding="utf-8")
        before = path.read_bytes() if path.is_file() else None
        error = None
        try:
            assert stages.classification_record(root, "recording", total) == total
        except portable.BundleError as caught:
            error = str(caught)
        assert (error is None) if expected is None else error == expected + " recording", (name, error)
        assert (path.read_bytes() if path.is_file() else None) == before
        results.append({"name": name, "passed": True, "units": total, "guard": error})
    real = []
    for kind, bundle in (("positive", args.positive_bundle), ("zero", args.zero_bundle)):
        if bundle is None:
            continue
        record = json.loads((bundle / "bundle.json").read_text())
        for recording in record["recording_ids"]:
            # The frozen outcome is an independent unit-count authority for the
            # CSV: never infer its expected cardinality from the measured table.
            total = record["metadata"]["outcomes"][recording]["analyzed_units"]
            assert type(total) is int and (total > 0 if kind == "positive" else total == 0)
            path = bundle / "data" / ("unit_classifier_" + recording + ".csv")
            before = path.read_bytes()
            assert stages.classification_record(bundle / "data", recording, total) == total
            assert path.read_bytes() == before
            real.append({"kind": kind, "recording": recording, "units": total,
                         "csv_sha256": hashlib.sha256(before).hexdigest()})
    summary = {"passed": True, "policy_controls": results, "real_input_policy_checks": real,
               "scientific_producers_executed": False, "full_bundle_API_or_NF_acceptance": False}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"passed": True, "policy_controls": len(results), "real_input_policy_checks": len(real)}))


if __name__ == "__main__":
    main()
