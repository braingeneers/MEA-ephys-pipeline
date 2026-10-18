"""Explicit POSIX file transport for portable standalone artifact workflows.

Recipe/service callers keep the default S3-only validators. Local references are
content-pinned regular files; published paths are create-only and root-bound.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import uuid

from .contracts import (ContractError, MAX_JSON_BYTES, assert_within, canonical_json,
                        decode_json, object_ref, parse_file, parse_s3, validate_ref, validate_root)
from .s3 import IntegrityError, ImmutableConflict, ObjectMissing, S3Error, S3Store


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class LocalStore:
    transport = "local"

    def __init__(self, output_root=None, s3_store=None, endpoint=None, profile=None):
        if output_root is not None:
            validate_root(output_root, self.transport)
        self.output_root = output_root
        self._s3_store = s3_store
        self._s3_options = {"endpoint": endpoint, "profile": profile}

    @property
    def _remote(self):
        # Pure local runs do not consult AWS credentials or an instance endpoint.
        if self._s3_store is None:
            self._s3_store = S3Store(**self._s3_options)
        return self._s3_store

    @staticmethod
    def _remote_uri(uri):
        if isinstance(uri, str) and uri[:3].lower() == "s3:":
            parse_s3(uri)
            return True
        parse_file(uri)
        return False

    @contextmanager
    def _parent(self, uri, create=False, publication=True):
        path = parse_file(uri)
        if create and publication:
            if self.output_root is None:
                raise ContractError("Publication requires a reviewed local output prefix")
            assert_within(uri, self.output_root, transport=self.transport)
        descriptor = None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open("/", flags)
            for part in path.parent.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, path.name
        except FileNotFoundError:
            raise ObjectMissing("Expected local object is missing") from None
        except OSError:
            raise S3Error("Local object I/O failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _reader(self, uri):
        with self._parent(uri) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise IntegrityError("Local references must identify regular files")
                yield stream
                try:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    raise IntegrityError("Local source changed during read") from None
                if (_stamp(before) != _stamp(os.fstat(stream.fileno()))
                        or _stamp(before) != _stamp(current)):
                    raise IntegrityError("Local source changed during read")

    def _read_local(self, uri, expected=None, limit=None, sink=None, collect=False):
        if expected is not None:
            validate_ref(expected, transport=self.transport)
            if expected["uri"] != uri:
                raise IntegrityError("Object reference URI differs from requested object")
        with self._reader(uri) as stream:
            announced = os.fstat(stream.fileno()).st_size
            if limit is not None and announced > limit:
                raise IntegrityError("Local object exceeds the metadata limit")
            if expected is not None and expected["size"] != announced:
                raise IntegrityError("Stored local object identity changed")
            count, sha256, chunks = 0, hashlib.sha256(), []
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if limit is not None and count > limit:
                    raise IntegrityError("Streamed local object exceeds the metadata limit")
                sha256.update(block)
                if sink is not None:
                    sink.write(block)
                if collect:
                    chunks.append(block)
            if count != announced:
                raise IntegrityError("Local object changed length during read")
            checksum = sha256.hexdigest()
            observed = object_ref(uri, count, "sha256-" + checksum, sha256=checksum,
                                  transport=self.transport)
            if expected is not None and any(expected[key] != observed[key]
                    for key in ("uri", "size", "etag", "sha256")):
                raise IntegrityError("Stored local object identity changed")
            return b"".join(chunks) if collect else None, observed

    def stat(self, uri):
        """Read local bytes to freeze their SHA-256, or use the exact S3 identity."""
        if self._remote_uri(uri):
            return self._remote.stat(uri)
        return self._read_local(uri)[1]

    def verify(self, ref):
        validate_ref(ref, transport=self.transport)
        if self._remote_uri(ref["uri"]):
            return self._remote.verify(ref)
        self._read_local(ref["uri"], expected=ref)
        return ref

    def verify_contents(self, ref):
        """Verify all bytes without making another local scientific-data copy."""
        validate_ref(ref, transport=self.transport)
        if self._remote_uri(ref["uri"]):
            return self._remote.verify_contents(ref)
        return self._read_local(ref["uri"], expected=ref)[1]

    @contextmanager
    def open_verified(self, ref):
        """Seek a local file only after a full hash, retaining change guards to close."""
        validate_ref(ref, transport=self.transport)
        if self._remote_uri(ref['uri']):
            raise ContractError('A local seekable read requires a file reference')
        with self._reader(ref['uri']) as stream:
            # Keep the outer descriptor's identity guard across both verification
            # and the caller's seeks. The inner full read verifies the frozen SHA.
            self._read_local(ref['uri'], expected=ref)
            yield stream

    def inspect_object(self, ref):
        """Local files have no S3 metadata; explicit S3 refs retain their metadata."""
        validate_ref(ref, transport=self.transport)
        if self._remote_uri(ref['uri']):
            return self._remote.inspect_object(ref)
        return {'object': self.verify_contents(ref), 'metadata': {}}

    def read_range(self, ref, offset, length):
        """Explicit S3 sources in a local graph use the same bounded range owner."""
        validate_ref(ref, transport=self.transport)
        if not self._remote_uri(ref['uri']):
            raise ContractError('Use open_verified for local seekable reads')
        return self._remote.read_range(ref, offset, length)

    def read_json(self, uri, expected=None, optional=False):
        if expected is not None and optional:
            raise ValueError("A frozen object reference must not be treated as optional")
        if self._remote_uri(uri):
            return self._remote.read_json(uri, expected=expected, optional=optional)
        try:
            data, observed = self._read_local(uri, expected=expected, limit=MAX_JSON_BYTES, collect=True)
        except ObjectMissing:
            if optional:
                return None
            raise
        return decode_json(data), observed

    def download(self, ref, destination):
        validate_ref(ref, transport=self.transport)
        remote = self._remote_uri(ref["uri"])
        destination = Path(os.path.abspath(destination))
        if not remote and destination.resolve() == parse_file(ref["uri"]).resolve():
            raise ContractError("Download destination must differ from its source")
        # A download target is explicit caller-owned scratch, possibly outside
        # the publication root. Still never replace an existing local object.
        with self._parent(destination.as_uri(), create=True, publication=False) as (parent, name):
            temporary = ".ephys-download-" + uuid.uuid4().hex + ".partial"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o666, dir_fd=parent)
            try:
                if remote:
                    os.close(descriptor)
                    observed = self._remote.download(ref, destination.parent / temporary)
                    descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    with os.fdopen(descriptor, 'rb') as completed:
                        if os.fstat(completed.fileno()).st_size != observed['size']:
                            raise IntegrityError('Local download changed before installation')
                        os.fsync(completed.fileno())
                else:
                    with os.fdopen(descriptor, "wb") as output:
                        _, observed = self._read_local(ref["uri"], expected=ref, sink=output)
                        output.flush()
                        os.fsync(output.fileno())
                expected = object_ref(destination.as_uri(), observed['size'],
                    'sha256-' + observed['sha256'], sha256=observed['sha256'], transport=self.transport)
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    try:
                        self._read_local(destination.as_uri(), expected=expected)
                    except S3Error:
                        raise ImmutableConflict('Existing download target differs or is not a regular file') from None
                os.fsync(parent)
                return observed
            finally:
                os.unlink(temporary, dir_fd=parent)

    def _publish(self, uri, writer):
        with self._parent(uri, create=True) as (parent, name):
            temporary = ".ephys-" + uuid.uuid4().hex + ".partial"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o666, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    size, checksum = writer(output)
                    output.flush()
                    os.fsync(output.fileno())
                ref = object_ref(uri, size, "sha256-" + checksum, sha256=checksum,
                                 transport=self.transport)
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    try:
                        self._read_local(uri, expected=ref)
                    except S3Error:
                        raise ImmutableConflict("Existing local object differs or is not a regular file") from None
                os.fsync(parent)
                return ref
            finally:
                os.unlink(temporary, dir_fd=parent)

    def put_json(self, uri, value):
        data = canonical_json(value)
        def writer(output):
            output.write(data)
            return len(data), hashlib.sha256(data).hexdigest()
        return self._publish(uri, writer)

    def upload_file(self, uri, source):
        source_uri = Path(source).resolve().as_uri()
        def writer(output):
            _, ref = self._read_local(source_uri, sink=output)
            return ref["size"], ref["sha256"]
        return self._publish(uri, writer)
