"""Offline reader installation; never resolve or upgrade scientific dependencies."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys


def versions():
    result = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"].lower().replace("_", "-")
        assert name not in result, ("Duplicate distribution", name)
        result[name] = distribution.version
    return result


def main():
    root = Path(__file__).resolve().parent
    lock = json.loads((root / "artifacts.lock.json").read_text())
    assert os.getuid() == 0, "Package installation requires the build user"
    assert Path("/sys/fs/cgroup/memory.max").read_text().strip() == "8589934592"
    assert Path("/sys/fs/cgroup/cpu.max").read_text().strip() == "200000 100000"
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ[name] = "2"
    os.environ["XDG_CACHE_HOME"] = "/tmp/mea-export-verifier-build-cache"
    os.environ["MPLCONFIGDIR"] = "/tmp/mea-export-verifier-build-mpl"
    before = versions()
    for name, wanted in lock["preserved_numerical_packages"].items():
        assert before[name] == wanted, (name, before.get(name), wanted)
    for name, wanted in lock["expected_parent_reader_packages"].items():
        assert before.get(name) == wanted, (name, before.get(name), wanted)
    wheels = root / "wheelhouse"
    assert {p.name for p in wheels.iterdir()} == {item["filename"] for item in lock["runtime"]}
    for item in lock["runtime"]:
        path = wheels / item["filename"]
        assert path.stat().st_size == item["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], path.name
    baseline = subprocess.run([sys.executable, "-m", "pip", "check"], text=True, capture_output=True)
    print(json.dumps(dict(parent_pip_check=dict(exit=baseline.returncode, stdout=baseline.stdout,
                                               stderr=baseline.stderr))), flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-index",
                    "--find-links", str(wheels), "--no-deps", "--require-hashes",
                    "-r", str(root / "requirements.lock")], check=True)
    after = versions()
    changes = {item["name"]: item["version"] for item in lock["runtime"]}
    assert set(after) == set(before) | set(changes)
    for name, wanted in changes.items():
        assert after[name] == wanted, (name, after.get(name), wanted)
    for name in set(before) - set(changes):
        assert after[name] == before[name], ("Unexpected package change", name)
    final = subprocess.run([sys.executable, "-m", "pip", "check"], text=True, capture_output=True)
    report = dict(parent_distributions=before, candidate_distributions=after,
                  baseline_pip_check=dict(exit=baseline.returncode, stdout=baseline.stdout, stderr=baseline.stderr),
                  candidate_pip_check=dict(exit=final.returncode, stdout=final.stdout, stderr=final.stderr),
                  build_uid=os.getuid(), memory_max=Path("/sys/fs/cgroup/memory.max").read_text().strip(),
                  cpu_max=Path("/sys/fs/cgroup/cpu.max").read_text().strip())
    (root / "build-check.json").write_text(json.dumps(report, indent=2) + "\n")
    assert final.returncode == 0, final.stdout + final.stderr
    plugin = Path(lock["plugin"]["path"])
    assert plugin.stat().st_size == lock["plugin"]["bytes"]
    assert hashlib.sha256(plugin.read_bytes()).hexdigest() == lock["plugin"]["sha256"]
    import pynwb
    import hdmf_zarr
    assert pynwb.__version__ == changes["pynwb"]
    assert hdmf_zarr.NWBZarrIO is not None
    print(json.dumps(dict(passed=True, exact_changes=changes, unchanged_distributions=len(set(before) - set(changes)),
                          baseline_pip_check_exit=baseline.returncode, candidate_pip_check_exit=final.returncode,
                          concrete_NWB_reader_imports=True, inherited_plugin_exact=True)))


if __name__ == "__main__":
    main()
