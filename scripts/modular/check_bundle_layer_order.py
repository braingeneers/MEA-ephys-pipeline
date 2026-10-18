#!/usr/bin/env python3
"""Check a real preprocessing visualization's layer order across packaging.

Supply the producer's preprocessedviz JSON and its copy inside a prepared (or
later) bundle. Uses actual capsule metadata; scientific reads/rendering are
covered separately by check_stage_science and replay_render_inputs.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline/helpers"))
import portable_bundle as bundle


def layer_order(value):
    return {recording: {mode: None if layers is None else list(layers)
                        for mode, layers in content["timeseries"].items()}
            for recording, content in value.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    original = json.loads(args.source.read_text())
    portable = json.loads(args.portable.read_text())
    before, after = layer_order(original), layer_order(portable)

    # Aliases may vary in whitespace; a producer's reordered plot rows must not
    # alias a different ordering solely because unordered JSON values match.
    target = args.output / "target.json"
    target.write_text(json.dumps(original, indent=2))
    formatted = args.output / "formatted.json"
    formatted.write_bytes(bundle.serialized_json(original))
    bundle.register_alias(formatted, target, {})
    changed = deepcopy(original)
    recording = next(iter(changed))
    layers = changed[recording]["timeseries"]["full"]
    assert len(layers) > 1, "Use a real multi-layer visualization fixture"
    changed[recording]["timeseries"]["full"] = dict(reversed(list(layers.items())))
    reordered = args.output / "reordered.json"
    reordered.write_text(json.dumps(changed))
    rejected = False
    try:
        bundle.register_alias(reordered, target, {})
    except bundle.BundleError:
        rejected = True
    result = {"passed": before == after and rejected, "source": str(args.source),
              "portable": str(args.portable), "source_layer_order": before,
              "portable_layer_order": after, "whitespace_alias_accepted": True,
              "reordered_layer_alias_rejected": rejected,
              "source_sha256": bundle.sha256(args.source),
              "portable_sha256": bundle.sha256(args.portable)}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
