"""Offline controls in the pinned Kilosort image; no sorting or model substitution.

Set KILOSORT_TEMPLATE_REFERENCE to an independently retrieved official wTEMP.npz.
The scientific control uses the installed Kilosort reader and compares every byte,
array and CPU tensor against that independent file.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


CODE = Path(__file__).resolve().parents[1] / 'capsules/kilosort4/code'
EXPECTED_SHA = 'cae1c96f8f4150be0a39627515750b70c4bc3548177cf487ae3c013f1ca6abd8'
SPEC = importlib.util.spec_from_file_location('prepare_templates', CODE/'prepare_templates.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class TemplateControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = Path(os.environ['KILOSORT_TEMPLATE_REFERENCE']).read_bytes()
        assert len(cls.reference) == 3432
        assert hashlib.sha256(cls.reference).hexdigest() == EXPECTED_SHA

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.asset = json.loads((CODE/'predefined_template.json').read_text())

    def write_asset(self, value):
        path = self.root/'asset.json'
        path.write_text(json.dumps(value))
        return path

    def test_official_payload_and_provenance(self):
        asset, payload = helper.read_asset(CODE/'predefined_template.json')
        self.assertEqual(payload, self.reference)
        self.assertEqual(asset['sha256'], EXPECTED_SHA)
        self.assertEqual(asset['source_url'], 'https://osf.io/download/6807fb5958b763aae139aa60/')
        self.assertEqual(asset['source_version'], 1)

    def test_fresh_and_verified_existing_copy(self):
        target, _ = helper.prepare_cache(self.root/'cache')
        stat = target.stat()
        again, _ = helper.prepare_cache(self.root/'cache')
        self.assertEqual(target.read_bytes(), self.reference)
        self.assertEqual((again.stat().st_ino, again.stat().st_mtime_ns), (stat.st_ino, stat.st_mtime_ns))

    def test_corrupt_payload_prevents_cache_creation(self):
        corrupt = self.reference[:-1]+bytes([self.reference[-1] ^ 1])
        self.asset['data_base64'] = base64.b64encode(corrupt).decode()
        with self.assertRaisesRegex(helper.TemplateAssetError, 'checksum mismatch'):
            helper.prepare_cache(self.root/'cache', self.write_asset(self.asset))
        self.assertFalse((self.root/'cache').exists())

    def test_missing_asset_prevents_cache_creation(self):
        with self.assertRaises(FileNotFoundError):
            helper.prepare_cache(self.root/'cache', self.root/'missing.json')
        self.assertFalse((self.root/'cache').exists())

    def test_corrupt_hash_is_rejected(self):
        self.asset['sha256'] = '0'*64
        with self.assertRaisesRegex(helper.TemplateAssetError, 'checksum mismatch'):
            helper.read_asset(self.write_asset(self.asset))

    def test_typed_record_and_encoding_controls(self):
        for key, value in [('schema_version', True), ('schema_version', 1.0),
                           ('bytes', 3432.0), ('source_version', True),
                           ('filename', '../wTEMP.npz'), ('data_base64', 'not base64!')]:
            with self.subTest(key=key, value=value):
                candidate = deepcopy(self.asset)
                candidate[key] = value
                with self.assertRaises(helper.TemplateAssetError):
                    helper.read_asset(self.write_asset(candidate))

    def test_corrupt_existing_cache_is_preserved_and_rejected(self):
        target, _ = helper.prepare_cache(self.root/'cache')
        target.write_bytes(b'corrupt')
        with self.assertRaisesRegex(helper.TemplateAssetError, 'Existing template cache checksum mismatch'):
            helper.prepare_cache(self.root/'cache')
        self.assertEqual(target.read_bytes(), b'corrupt')

    def test_symlink_cache_is_rejected(self):
        outside = self.root/'outside'
        outside.write_bytes(self.reference)
        (self.root/'cache').mkdir()
        (self.root/'cache/wTEMP.npz').symlink_to(outside)
        with self.assertRaises(helper.TemplateAssetError):
            helper.prepare_cache(self.root/'cache')
        self.assertEqual(outside.read_bytes(), self.reference)

    def test_concurrent_preparation_retains_exact_input(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: helper.prepare_cache(self.root/'cache'), range(4)))
        self.assertEqual(len(results), 4)
        self.assertEqual((self.root/'cache/wTEMP.npz').read_bytes(), self.reference)
        self.assertEqual(sorted(p.name for p in (self.root/'cache').iterdir()), ['wTEMP.npz'])

    def test_installed_template_reader_and_tensors_match(self):
        import numpy as np
        import torch
        from kilosort import spikedetect, utils

        cache, _ = helper.prepare_cache(self.root/'cache')
        # Both branches invoke the installed Kilosort function. Its downloads dir
        # is its documented control; no sorter/reader function is replaced.
        original = utils.DOWNLOADS_DIR
        self.addCleanup(setattr, utils, 'DOWNLOADS_DIR', original)
        legacy = self.root/'independent-legacy-cache'
        legacy.mkdir()
        (legacy/'wTEMP.npz').write_bytes(self.reference)
        values = []
        for directory in [legacy, cache.parent]:
            utils.DOWNLOADS_DIR = directory
            self.assertEqual(Path(utils.template_path()).read_bytes(), self.reference)
            values.append(spikedetect.get_waves({}, device=torch.device('cpu')))
        with np.load(legacy/'wTEMP.npz', allow_pickle=False) as official:
            self.assertEqual(set(official.files), {'wTEMP', 'wPCA'})
            for index, name in enumerate(['wPCA', 'wTEMP']):
                expected = official[name]
                self.assertTrue(np.isfinite(expected).all())
                for result in values:
                    array = result[index].numpy()
                    self.assertEqual(array.shape, expected.shape)
                    self.assertEqual(array.dtype, expected.dtype)
                    self.assertEqual(array.tobytes(), expected.tobytes())
                self.assertTrue(torch.equal(values[0][index], values[1][index]))

    def wrapper_fixture(self):
        directory = self.root/'capsule-code'
        directory.mkdir()
        for name in ['run', 'prepare_templates.py', 'predefined_template.json']:
            shutil.copyfile(CODE/name, directory/name)
        (directory/'run_capsule.py').write_text('''
import hashlib, json, os, pathlib, sys
from kilosort import utils
p=pathlib.Path(utils.template_path())
pathlib.Path('called.json').write_text(json.dumps(dict(args=sys.argv[1:],
    path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())))
''')
        return directory

    def test_launch_prepares_before_real_kilosort_import_and_preserves_arguments(self):
        directory = self.wrapper_fixture()
        arguments = ['--params', '{"sorter":{"templates_from_data":false,"Th_universal":9}}']
        result = subprocess.run(['bash', 'run']+arguments, cwd=directory, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads((directory/'called.json').read_text())
        self.assertEqual(observed['args'], arguments)
        self.assertEqual(observed['sha256'], EXPECTED_SHA)
        self.assertEqual(Path(observed['path']), directory/'.kilosort-downloads/wTEMP.npz')

    def test_corrupt_launch_asset_prevents_scientific_command(self):
        directory = self.wrapper_fixture()
        self.asset['sha256'] = '0'*64
        (directory/'predefined_template.json').write_text(json.dumps(self.asset))
        result = subprocess.run(['bash', 'run'], cwd=directory, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Template asset checksum mismatch', result.stderr)
        self.assertFalse((directory/'called.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
