#!/usr/bin/env python3
"""Execute one stub report with NRP selectors and a local-executor overlay."""
import argparse
import csv
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    code = root / "code"
    code.mkdir()
    (root / "analyzer").mkdir()
    (code / "run_capsule.py").write_text("""import argparse
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--analyzer-dir'); p.add_argument('--output-dir'); p.add_argument('--thresholds')
a=p.parse_args(); Path(a.output_dir,'binding.txt').write_text('report selector passed')
""")
    settings = {"container": args.image, "results_path": str(root / "published"),
                "publish_overwrite": None}
    script = ("nextflow.enable.dsl=2\ninclude { report_generation } from "
              + json.dumps(str(source / "pipeline/modules/report_generation")) + "\n"
              + "workflow { report_generation(channel.value(new groovy.json.JsonSlurper().parseText("
              + json.dumps(json.dumps(settings)) + ")), channel.value(tuple('probe', file("
              + json.dumps(str(root / "analyzer")) + "))), file("
              + json.dumps(str(code)) + ")) }\n")
    (root / "main.nf").write_text(script)
    # Enable the real task image: a bare local executor reports no container
    # identity and cannot prove that the NRP selector uses the input settings.
    (root / "local.config").write_text("process.executor='local'\ndocker.enabled=true\nshifter.enabled=false\n"
                                       "docker.runOptions='--user 1000:1000 -e HOME=/tmp'\n")
    command = ["nextflow", "-C", str(source / "pipeline/nextflow_nrp.config") + "," + str(root / "local.config"),
               "run", str(root / "main.nf"), "-profile", "gpu", "-with-trace", str(root / "trace.tsv")]
    (root / "command.json").write_text(json.dumps(command, indent=2))
    with (root / "run.log").open("w") as log:
        subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    with (root / "trace.tsv").open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 1 and rows[0]["exit"] == "0", rows
    assert rows[0]["container"] == args.image, rows[0]
    assert (root / "published/reports/probe/binding.txt").read_text() == "report selector passed"
    result = {"passed": True, "task": rows[0], "cluster_work_submitted": False,
              "scientific_capsule_stubbed": True, "executor": "local"}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
