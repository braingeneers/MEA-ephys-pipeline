"""Inspect installed scientific dependencies and the owned tool/image boundary."""
import hashlib
import importlib
from importlib import metadata
import json
from pathlib import Path
import platform

from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin, require_hdf5_filter_capabilities
from workflow_defaults import scientific_defaults


def identity():
    root = Path(__file__).resolve().parents[1]
    extraction = json.loads((root / 'extraction.json').read_text())
    lock = json.loads((root / 'runtime-lock.json').read_text())
    modules, tool_sha256 = {}, {}
    owned_files = {**extraction['scripts'], **extraction.get('owned_helpers', {})}
    for filename, record in owned_files.items():
        module = importlib.import_module(Path(filename).stem)
        path = Path(module.__file__).resolve()
        if path != root / 'scripts' / filename:
            raise RuntimeError('Scientific tool resolved outside its owning image: ' + filename)
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha256 != record['extracted_sha256']:
            raise RuntimeError('Scientific tool differs from its extraction record: ' + filename)
        modules[filename] = str(path)
        tool_sha256[filename] = actual_sha256
    plugin_dir = configure_bundled_maxwell_hdf5_plugin()
    require_hdf5_filter_capabilities(401, require_decode=True, require_encode=True)
    plugin = Path(plugin_dir) / 'libcompression.so'
    dependencies = {}
    for name in lock['dependencies']:
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    direct = json.loads(metadata.distribution('ndx-cell-culture').read_text('direct_url.json'))
    observed = {'python': platform.python_version(), 'dependencies': dependencies,
        'plugin_sha256': hashlib.sha256(plugin.read_bytes()).hexdigest(),
        'plugin_size': plugin.stat().st_size,
        'ndx_cell_culture_commit': direct['vcs_info']['commit_id']}
    if observed != lock:
        raise RuntimeError('The scientific runtime differs from its pinned baseline lock.')
    return {**observed, 'identity_version': 2, 'tool_sha256': tool_sha256,
        'extraction_sha256': hashlib.sha256((root / 'extraction.json').read_bytes()).hexdigest(),
        'source_commit': extraction['source_commit'], 'tool_paths': modules,
        'filter_401_encode_decode': True, 'defaults': scientific_defaults()}


if __name__ == '__main__':
    print(json.dumps(identity(), indent=2))
