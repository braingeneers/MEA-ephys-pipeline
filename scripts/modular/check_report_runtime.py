#!/usr/bin/env python3
"""Verify the report image changes only its two pinned spreadsheet dependencies."""
import argparse
import json
from pathlib import Path
import subprocess


def packages(image):
    command = ["docker", "run", "--rm", "--network", "none", "-e", "HOME=/tmp",
               "--entrypoint", "python", image, "-c",
               "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}))"]
    return json.loads(subprocess.check_output(command, text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="braingeneers/mea-ephys-analysis:0.1.0")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    base = json.loads((source / "scripts/nrp/images.json").read_text())["base"]
    before, after = packages(base), packages(args.image)
    changes = {name: [before.get(name), after.get(name)] for name in before.keys() | after.keys()
               if before.get(name) != after.get(name)}
    assert changes == {"openpyxl": [None, "3.1.5"], "et-xmlfile": [None, "2.0.0"]}, changes
    subprocess.run(["docker", "run", "--rm", "--network", "none", "-e", "HOME=/tmp",
                    "--entrypoint", "python", args.image, "-c",
                    "from io import BytesIO; from openpyxl import Workbook,load_workbook; "
                    "b=BytesIO(); w=Workbook(); w.active.append(['unit',7]); w.save(b); "
                    "b.seek(0); assert load_workbook(b).active['B1'].value==7; print('Spreadsheet round trip: PASS')"],
                   check=True)
    identity = subprocess.check_output(["docker", "image", "inspect", args.image,
                                        "--format", "{{.Id}}"], text=True).strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": True, "base": base, "image": args.image,
                                      "local_image_id": identity, "changes": changes,
                                      "network_disabled": True, "runtime_home": "/tmp"}, indent=2) + "\n")
    print(json.dumps({"passed": True, "changes": changes, "image_id": identity}))


if __name__ == "__main__":
    main()
