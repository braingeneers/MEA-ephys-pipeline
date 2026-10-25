"""Prepare the checksum-pinned numerical input used by Kilosort's preset mode.

This runs before Kilosort imports its downloads-path environment variable. It
does not import or modify the sorter and never retrieves data from the network.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


class TemplateAssetError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TemplateAssetError(message)


def read_asset(path):
    asset = json.loads(Path(path).read_text())
    require(isinstance(asset, dict) and set(asset) == {
        'schema_version', 'kilosort_version', 'filename', 'bytes', 'sha256',
        'source_url', 'source_version', 'data_base64'}, 'Invalid template asset record')
    require(type(asset['schema_version']) is int and asset['schema_version'] == 1
            and asset['kilosort_version'] == '4.0.38'
            and asset['filename'] == 'wTEMP.npz', 'Unsupported template asset')
    require(type(asset['bytes']) is int and asset['bytes'] > 0
            and type(asset['source_version']) is int and asset['source_version'] == 1
            and isinstance(asset['source_url'], str)
            and asset['source_url'] == 'https://osf.io/download/6807fb5958b763aae139aa60/'
            and isinstance(asset['sha256'], str)
            and re.fullmatch(r'[0-9a-f]{64}', asset['sha256']), 'Invalid template asset identity')
    require(isinstance(asset['data_base64'], str), 'Invalid template payload encoding')
    try:
        payload = base64.b64decode(asset['data_base64'], validate=True)
    except ValueError as error:
        raise TemplateAssetError('Invalid template payload encoding') from error
    require(len(payload) == asset['bytes']
            and hashlib.sha256(payload).hexdigest() == asset['sha256'],
            'Template asset checksum mismatch')
    return asset, payload


def prepare_cache(cache_dir, asset_path=None):
    asset, payload = read_asset(asset_path or Path(__file__).with_name('predefined_template.json'))
    cache = Path(cache_dir)
    require(not cache.is_symlink(), 'Template cache directory must not be a symlink')
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / asset['filename']

    def verify_existing():
        require(not target.is_symlink() and target.is_file()
                and target.stat().st_size == asset['bytes']
                and target.read_bytes() == payload, 'Existing template cache checksum mismatch')

    if target.exists() or target.is_symlink():
        verify_existing()
    else:
        # A consumer sees the complete file, including if another invocation wins.
        with tempfile.NamedTemporaryFile(dir=cache, prefix='.template-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
                verify_existing()
            finally:
                temporary.unlink()
    return target, asset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', required=True)
    args = parser.parse_args()
    target, asset = prepare_cache(args.cache_dir)
    print('Verified Kilosort predefined template: {} bytes; sha256={}; {}'.format(
        asset['bytes'], asset['sha256'], target))


if __name__ == '__main__':
    main()
