"""Shared approved-image guard for fresh, reused and adopted Maxwell science."""
import json
from pathlib import Path

from ephys_modular.contracts import ContractError, digest
from runtime_identity import identity


def validate_owner_runtime(owner, scientific_runtime=None):
    """Bind an approved image to its independently measured installed tools.

    Source image metadata alone cannot identify a locally overridden container.
    Historical images without a recorded fingerprint retain their old checks.
    """
    rules = json.loads(Path(__file__).with_name('owner-runtime-policy.json').read_text())
    expected = rules['runtime_contracts'].get(owner['image'])
    if expected is not None:
        scientific_runtime = identity() if scientific_runtime is None else scientific_runtime
        if digest(scientific_runtime) != expected:
            raise ContractError('Selected workflow image differs from the installed scientific runtime; '
                'use its pinned image or a separately validated owner/runtime pair')
    return scientific_runtime
