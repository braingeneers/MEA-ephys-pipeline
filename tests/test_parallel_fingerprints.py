"""Exact scientific receipts and lifecycle checks for concurrent validation.

Run in the pinned scientific base image with NWB_IDENTITY_FIXTURE generated and
validated by create_identity_nwb.py in the pinned native NWB image. These small
fixtures test the bundle contract; they do not replace full-run acceptance.
"""
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import h5py
import numpy as np
import spikeinterface.preprocessing as preprocessing
from spikeinterface.extractors.nwbextractors import NwbRecordingExtractor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/helpers"))
import portable_bundle as portable


class ParallelFingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Path(os.environ["NWB_IDENTITY_FIXTURE"])
        if not cls.fixture.is_file():
            raise ValueError("Generate and validate NWB_IDENTITY_FIXTURE in the native image first")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.nwb = self.source / "raw.nwb"
        shutil.copyfile(self.fixture, self.nwb)
        self.original_nwb_sha256 = portable.sha256(self.nwb)
        self.graph_path = self.source / "recordings.json"
        raw = NwbRecordingExtractor(
            self.nwb, electrical_series_path="acquisition/ElectricalSeries",
            load_time_vector=False, cache=False)
        highpass = preprocessing.highpass_filter(raw, freq_min=300, margin_ms=5, dtype="int16")
        selected = highpass.select_channels([f"ch{i}" for i in range(16) if i not in (3, 11)])
        processed = preprocessing.common_reference(selected, reference="global", operator="median")
        processed = processed.select_channels([f"ch{i}" for i in (15, 0, 2, 4, 8, 12)])
        # Multiple pointers share a file. Non-sorted order is observable to the
        # real capsule consumers and must survive each sequential graph rewrite.
        self.graphs = {
            "views": {
                "z_processed": processed.to_dict(recursive=True),
                "a_raw": raw.to_dict(recursive=True),
            },
            "annotation": {"first": -0.0, "second": None, "third": 9007199254740997},
        }
        portable.write_serialization(self.graph_path, self.graphs)
        self.spec = {
            "schema_version": 1,
            "unit_id": "parallel-contract-fixture",
            "stage": "nwb",
            "recording_ids": ["fixture"],
            "payloads": [{"source": str(self.source), "destination": "data"}],
            "serializations": [self.descriptor("z_processed"), self.descriptor("a_raw")],
            "metadata": {"test_scope": "contents-only parallel validation contract"},
        }
        self.children_before = {child.pid for child in multiprocessing.active_children()}
        self.addCleanup(self.assert_no_new_children)

    def descriptor(self, name):
        return {
            "format": "extractor_json", "source": str(self.graph_path),
            "path": "data/recordings.json", "base": str(self.source),
            "relative_to": "data", "pointer": ["views", name],
        }

    def assert_no_new_children(self):
        deadline = time.monotonic() + 3
        while True:
            children = [child for child in multiprocessing.active_children()
                        if child.pid not in self.children_before]
            if not children or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.assertEqual([], [(child.pid, child.name) for child in children],
                         "Validation left live child processes")

    def pack(self, name, workers=1):
        output = self.root / name
        record = portable.pack(deepcopy(self.spec), output, workers=workers)
        self.assertEqual(self.original_nwb_sha256, portable.sha256(self.nwb))
        return output, record

    def reseal_transport(self, output, record):
        # Deliberately retain the original scientific and executable receipts.
        # A receiver may reseal transport bytes; it cannot recreate expectations.
        record["files"] = portable.inventory(output / "data")
        record["bytes"] = sum(item["bytes"] for item in record["files"].values())
        record.pop("content_sha256")
        record["content_sha256"] = hashlib.sha256(portable.encoded(record)).hexdigest()
        portable.write_manifest(output / "bundle.json", record)

    def test_serial_and_parallel_complete_receipts_are_exact(self):
        serial_root, serial = self.pack("serial", workers=1)
        parallel_root, parallel = self.pack("parallel", workers=2)
        self.assertEqual(portable.encoded(serial), portable.encoded(parallel))
        self.assertEqual([item["pointer"] for item in self.spec["serializations"]],
                         [item["pointer"] for item in parallel["serializations"]])
        identities = parallel["scientific_fingerprints"]
        self.assertEqual([6, 16], [len(item["recording"]["channel_ids"]) for item in identities])
        self.assertNotEqual(identities[0], identities[1])
        for identity in identities:
            recording = identity["recording"]
            self.assertEqual(65536, recording["read_policy"]["frames_per_block"])
            self.assertEqual(131251, recording["segments"][0]["frames"])
            for stream in ("raw", "microvolts", "times"):
                self.assertEqual(131251, recording["segments"][0][stream]["shape"][0])
        self.assertEqual(portable.encoded(portable.verify(serial_root, workers=1)),
                         portable.encoded(portable.verify(parallel_root, workers=2)))
        self.assertTrue(portable.verify_science(parallel_root, parallel, workers=2)["passed"])
        self.assert_no_new_children()

    def test_shared_serialization_preserves_all_pointers_and_key_order(self):
        output, record = self.pack("shared-pointers", workers=2)
        value = portable.read_serialization(output / "data/recordings.json")
        self.assertEqual(["views", "annotation"], list(value))
        self.assertEqual(["z_processed", "a_raw"], list(value["views"]))
        self.assertEqual(portable.serialized_json(self.graphs["annotation"]),
                         portable.serialized_json(value["annotation"]))
        for descriptor in record["serializations"]:
            graph = portable.at_pointer(value, descriptor["pointer"])
            self.assertIs(graph["relative_paths"], True)
            portable.assert_local_extractor(graph, output / "data", output)
        self.assertTrue(portable.verify(output, workers=2)["passed"])

    def test_parallel_verification_rejects_late_frame_corruption(self):
        output, record = self.pack("late-corruption")
        with h5py.File(output / "data/raw.nwb", "r+") as nwb:
            signal = nwb["acquisition/ElectricalSeries/data"]
            self.assertEqual(131251, signal.shape[0])
            # Interior data beyond the first block and outside the historical
            # first/middle/final short sampling windows must still be checked.
            signal[100003, 3] = np.int16(signal[100003, 3] + 1)
        self.reseal_transport(output, record)
        portable.verify_manifest(output)
        with self.assertRaisesRegex(portable.BundleError, "scientific fingerprint mismatch"):
            portable.verify(output, workers=2)
        self.assertEqual(self.original_nwb_sha256, portable.sha256(self.nwb))

    def test_parallel_verification_rejects_clock_corruption(self):
        output, record = self.pack("clock-corruption")
        with h5py.File(output / "data/raw.nwb", "r+") as nwb:
            starting_time = nwb["acquisition/ElectricalSeries/starting_time"]
            starting_time[()] = starting_time[()] + 0.000125
        self.reseal_transport(output, record)
        portable.verify_manifest(output)
        with self.assertRaisesRegex(portable.BundleError, "scientific fingerprint mismatch"):
            portable.verify(output, workers=2)
        self.assertEqual(self.original_nwb_sha256, portable.sha256(self.nwb))

    def test_worker_error_does_not_publish_manifest_or_leave_children(self):
        broken = deepcopy(self.graphs)
        broken["views"]["a_raw"]["kwargs"]["electrical_series_path"] = "acquisition/AbsentElectricalSeries"
        portable.write_serialization(self.graph_path, broken)
        output = self.root / "worker-error"
        with self.assertRaises(Exception) as caught:
            portable.pack(deepcopy(self.spec), output, workers=2)
        self.assertIn("AbsentElectricalSeries", str(caught.exception),
                      "Worker error lost its causal acquisition identity")
        rewritten = portable.read_serialization(output / "data/recordings.json")
        self.assertIs(rewritten["views"]["a_raw"]["relative_paths"], True)
        self.assertFalse((output / "bundle.json").exists())
        self.assertEqual(self.original_nwb_sha256, portable.sha256(self.nwb))
        self.assert_no_new_children()
        # A worker failure must not poison a later valid operation in this process.
        portable.write_serialization(self.graph_path, self.graphs)
        recovered, _ = self.pack("after-worker-error", workers=2)
        self.assertTrue(portable.verify(recovered, workers=2)["passed"])

    def test_cli_uses_and_validates_existing_cpu_budget(self):
        output, record = self.pack("cli-budget-reference")
        command = [sys.executable, portable.__file__, "verify", "--bundle", str(output)]
        for value in ("0", "-2", "true", "1.0", "", " 2 "):
            with self.subTest(cpu_budget=value):
                environment = {**os.environ, "CO_CPUS": value}
                result = subprocess.run(command, capture_output=True, text=True,
                                        env=environment, timeout=30)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("CO_CPUS must be a positive integer", result.stderr)
                self.assertFalse(result.stdout.strip())
        # More allocated CPUs than descriptors must remain valid. Omission uses
        # the ordinary portable one-worker path; neither changes receipt bytes.
        for value in (None, "32"):
            with self.subTest(cpu_budget=value):
                environment = os.environ.copy()
                environment.pop("CO_CPUS", None)
                if value is not None:
                    environment["CO_CPUS"] = value
                result = subprocess.run(command, capture_output=True, text=True,
                                        env=environment, timeout=30)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(record["content_sha256"],
                                 json.loads(result.stdout)["content_sha256"])
        self.assert_no_new_children()

    def test_abrupt_worker_exit_fails_cli_without_manifest_or_live_children(self):
        # Fault injection is confined to our own child inside disposable Docker.
        # It neither sends signals to Kubernetes workloads nor force-deletes Pods.
        spec = self.root / "crash-spec.json"
        spec.write_text(json.dumps(self.spec))
        output = self.root / "abrupt-worker-exit"
        command = [sys.executable, portable.__file__, "pack", "--spec", str(spec),
                   "--output", str(output)]
        process = subprocess.Popen(command, env={**os.environ, "CO_CPUS": "2"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)

        def live_group():
            result = []
            for path in Path("/proc").glob("[0-9]*/stat"):
                try:
                    fields = path.read_text().rsplit(")", 1)[1].split()
                    if int(fields[2]) == process.pid and fields[0] != "Z":
                        result.append(int(path.parent.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue
            return result

        try:
            killed = None
            deadline = time.monotonic() + 20
            while process.poll() is None and time.monotonic() < deadline and killed is None:
                children = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                try:
                    pids = children.read_text().split()
                except FileNotFoundError:
                    break
                for text_pid in pids:
                    try:
                        args = Path(f"/proc/{text_pid}/cmdline").read_bytes()
                        if b"spawn_main" in args and b"--multiprocessing-fork" in args:
                            os.kill(int(text_pid), signal.SIGKILL)
                            killed = int(text_pid)
                            break
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                time.sleep(0.01)
            self.assertIsNotNone(killed, "No owned spawned worker was faulted")
            stdout, stderr = process.communicate(timeout=15)
            self.assertNotEqual(0, process.returncode, stdout)
            self.assertRegex(stderr, "BrokenProcessPool|terminated abruptly")
            self.assertFalse((output / "bundle.json").exists())
            deadline = time.monotonic() + 3
            while live_group() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual([], live_group(), "CLI left live processes after worker death")
        finally:
            if process.poll() is None or live_group():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)
        self.assertEqual(self.original_nwb_sha256, portable.sha256(self.nwb))

    def test_invalid_worker_budgets_are_rejected(self):
        output, record = self.pack("worker-budget-reference")
        for index, workers in enumerate((0, -1, True, False, 1.0, "2", None)):
            with self.subTest(workers=workers):
                with self.assertRaises(portable.BundleError):
                    portable.pack(deepcopy(self.spec), self.root / f"invalid-{index}", workers=workers)
                with self.assertRaises(portable.BundleError):
                    portable.verify_science(output, record, workers=workers)
                with self.assertRaises(portable.BundleError):
                    portable.verify(output, workers=workers)
        self.assert_no_new_children()


if __name__ == "__main__":
    unittest.main()
