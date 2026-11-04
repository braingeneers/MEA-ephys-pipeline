"""Operation-scoped content evidence for read-only metadata construction.

Use ``verified_inputs`` for one operation, or ``metadata_inputs`` on a metadata
entry point whose first argument is its store. Generic store semantics remain
untouched. Only references and metadata, never scientific bytes, are retained.
"""
from contextlib import contextmanager
import copy
from functools import wraps
import os
import threading

from .contracts import ContractError, canonical_json, validate_ref
from .s3 import IntegrityError

__all__ = ['verified_inputs', 'metadata_inputs']

MAX_ENTRIES = 128
MAX_BYTES = 1024 * 1024


def _identity(reference):
    return tuple(reference.get(k) for k in ('uri', 'size', 'etag', 'version_id'))


class _VerifiedInputs:
    """One thread/process's read-only metadata operation, with bounded references."""
    def __init__(self, store, *, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES):
        if store.transport != 's3':
            raise ContractError('Verified input sessions require the S3 conditional identity API')
        for value, limit in ((max_entries, MAX_ENTRIES), (max_bytes, MAX_BYTES)):
            if type(value) is not int or not 0 <= value <= limit:
                raise ContractError('Invalid verified-input bookkeeping budget')
        self._store = store
        # An inspected source can already influence a lifecycle/role decision
        # before its first full read. Retain that metadata observation too.
        self._entries = {}
        self._encoded_bytes = 0
        self._max_entries, self._max_bytes = max_entries, max_bytes
        self._owner = (os.getpid(), threading.get_ident())
        self._closed = False
        self._failed = False
        self.counts = dict(full_verifications=0, reuse_hits=0, identity_checks=0,
                           uncached_full_verifications=0)

    def _active(self):
        if self._closed or self._failed or self._owner != (os.getpid(), threading.get_ident()):
            raise ContractError('Verified-input operation is closed, failed or belongs to another owner')

    def _inspect(self, reference):
        self.counts['identity_checks'] += 1
        observed = self._store.inspect_object(reference)
        validate_ref(observed['object'])
        if _identity(reference)[:3] != _identity(observed['object'])[:3]:
            raise IntegrityError('Input conditional identity changed')
        if 'version_id' in reference and reference['version_id'] != observed['object'].get('version_id'):
            raise IntegrityError('Input conditional version changed')
        observed = copy.deepcopy(observed)
        self._remember(observed['object'], canonical_json(observed['metadata']))
        return observed

    def _remember(self, reference, metadata):
        key = _identity(reference)
        previous = self._entries.get(key)
        old_size = 0
        if previous is not None:
            old, old_metadata = previous
            if metadata != old_metadata:
                raise IntegrityError('Input metadata changed inside the verification operation')
            if 'sha256' in old:
                if reference.get('sha256', old['sha256']) != old['sha256']:
                    raise IntegrityError('Input SHA-256 differs from the completed verification')
                return True
            old_size = len(canonical_json(old)) + len(old_metadata)
        size = len(canonical_json(reference)) + len(metadata)
        if ((previous is not None or len(self._entries) < self._max_entries)
                and self._encoded_bytes - old_size + size <= self._max_bytes):
            self._entries[key] = (copy.deepcopy(reference), metadata)
            self._encoded_bytes += size - old_size
            return True
        return False

    def inspect_object(self, reference):
        self._active()
        try:
            validate_ref(reference)
            return self._inspect(reference)
        except BaseException:
            self._fail()
            raise

    def _fail(self):
        self._failed = True
        self._entries.clear()
        self._encoded_bytes = 0

    def verify_contents(self, reference):
        self._active()
        try:
            validate_ref(reference)
            observed = self._inspect(reference)
            frozen = observed['object']
            if 'sha256' in reference:
                frozen['sha256'] = reference['sha256']
            key = _identity(frozen)
            present = self._entries.get(key)
            metadata = canonical_json(observed['metadata'])
            if present is not None and 'sha256' in present[0]:
                full, original_metadata = present
                if metadata != original_metadata:
                    raise IntegrityError('Input metadata changed inside the verification operation')
                if reference.get('sha256', full['sha256']) != full['sha256']:
                    raise IntegrityError('Input SHA-256 differs from the completed verification')
                self.counts['reuse_hits'] += 1
                return copy.deepcopy(full)
            # Pin the observed version for this full read, including legacy
            # callers that supplied only URI/size/ETag. HEAD never supplies SHA.
            full = self._store.verify_contents(frozen)
            validate_ref(full, strong=True)
            if _identity(full) != key or reference.get('sha256', full['sha256']) != full['sha256']:
                raise IntegrityError('Completed input differs from the conditional reference')
            after = self._inspect(full)
            if _identity(after['object']) != key or canonical_json(after['metadata']) != metadata:
                raise IntegrityError('Input identity or metadata changed during full verification')
            self.counts['full_verifications'] += 1
            if not self._remember(full, metadata):
                # No new scientific input limit or eviction-based trust. Full
                # verification remains available when bookkeeping cannot retain it.
                self.counts['uncached_full_verifications'] += 1
            return copy.deepcopy(full)
        except BaseException:
            self._fail()
            raise

    def finish(self):
        self._active()
        try:
            for full, metadata in self._entries.values():
                current = self._inspect(full)
                if _identity(current['object']) != _identity(full) or canonical_json(current['metadata']) != metadata:
                    raise IntegrityError('Verified input changed before the metadata operation returned')
        except BaseException:
            self._fail()
            raise

    def close(self):
        self._entries.clear()
        self._encoded_bytes = 0
        self._closed = True

    def __getattr__(self, name):
        self._active()
        if name in ('download', 'put_json', 'upload_file'):
            raise ContractError('Use the original store for scientific transfer or publication')
        return getattr(self._store, name)


@contextmanager
def verified_inputs(store, **budgets):
    """Borrow nested evidence, finish/clear the outer owner, or pass local through.

    Bookkeeping overflow falls back to complete reads. A failed/cancelled body
    poisons the view. Only the creating thread/process may use it; evidence never
    crosses operations. Scientific transfer/publication needs the original store.
    """
    if isinstance(store, _VerifiedInputs):
        store._active()
        if budgets:
            raise ContractError('Nested operations must retain their owning bookkeeping budget')
        try:
            yield store
        except BaseException:
            store._fail()
            raise
        return
    if store.transport != 's3':
        yield store
        return
    view = _VerifiedInputs(store, **budgets)
    try:
        yield view
        view.finish()
    except BaseException:
        view._fail()
        raise
    finally:
        view.close()


def metadata_inputs(function):
    """Scope the store argument of a read-only metadata entry point."""
    @wraps(function)
    def checked(store, *args, **kwargs):
        with verified_inputs(store) as inputs:
            return function(inputs, *args, **kwargs)
    return checked
