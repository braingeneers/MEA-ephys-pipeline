"""One explicit retry budget per idempotent operation; compatible with botocore 1.16."""

import base64
from contextlib import contextmanager
import errno
import hashlib
import logging
import math
import os
from pathlib import Path
import random
import tempfile
import time
import uuid

from botocore.config import Config
from botocore import exceptions as botocore_errors
from botocore.exceptions import ClientError
import botocore.session
from urllib3.exceptions import ProtocolError, ReadTimeoutError as UrllibReadTimeout

from . import TRANSFER_FAILURE_EXIT
from .contracts import (MAX_JSON_BYTES, ContractError, assert_within, canonical_json,
                        decode_json, object_ref, parse_s3, validate_ref)

LOG = logging.getLogger(__name__)
MIN_PART_BYTES = 5 * 1024 * 1024
PART_BYTES = 64 * 1024 * 1024
MAX_PARTS = 10_000
MAX_RANGE_BYTES = 8 * 1024 * 1024
CLAIM_SUFFIX = ".ephys-upload.json"
_TRANSIENT = tuple(getattr(botocore_errors, name) for name in (
    "ConnectionClosedError", "ConnectTimeoutError", "EndpointConnectionError",
    "ReadTimeoutError", "ResponseStreamingError", "IncompleteReadError")
                   if hasattr(botocore_errors, name)) + (ProtocolError, UrllibReadTimeout)
_SDK_CHECKSUM_ERRORS = tuple(getattr(botocore_errors, name) for name in
                            ('FlexibleChecksumError', 'ChecksumError') if hasattr(botocore_errors, name))


class S3Error(RuntimeError):
    exit_code = TRANSFER_FAILURE_EXIT


class RetryExhausted(S3Error):
    pass


class ObjectMissing(S3Error):
    pass


class IntegrityError(S3Error):
    pass


class ImmutableConflict(IntegrityError):
    pass


class UploadUncertain(S3Error):
    """No new mutation is safe without checking this exact upload/object first."""


def _code(error):
    return str(error.response.get("Error", {}).get("Code", "")) if isinstance(error, ClientError) else ""


def _missing(error):
    return _code(error) in {"404", "NoSuchKey", "NotFound"}


def _transient(error):
    if isinstance(error, botocore_errors.SSLError):
        reason = str(error.kwargs.get("error", ""))
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            return False
        # Retry interrupted TLS streams, never an invalid/untrusted certificate.
        return any(text in reason for text in (
            "UNEXPECTED_EOF_WHILE_READING", "EOF occurred in violation of protocol",
            "TLSV1_ALERT_INTERNAL_ERROR"))
    if isinstance(error, ClientError):
        status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        return status in {408, 429, 500, 502, 503, 504} or _code(error) in {
            "SlowDown", "RequestTimeout", "Throttling", "InternalError", "ServiceUnavailable"}
    if isinstance(error, _TRANSIENT + (ConnectionError, TimeoutError)):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, errno.EPIPE,
        errno.ENETUNREACH, errno.EHOSTUNREACH}


class RetryPolicy:
    def __init__(self, attempts=6, initial_delay=2, maximum_delay=30, jitter=1,
                 sleep=time.sleep, on_retry=None):
        if type(attempts) is not int or attempts < 1:
            raise ValueError("Retry attempts must be a positive integer")
        if any(not math.isfinite(n) or n < 0 for n in (initial_delay, maximum_delay, jitter)):
            raise ValueError("Retry delays must be finite nonnegative values")
        self.attempts = attempts
        self.initial_delay = initial_delay
        self.maximum_delay = maximum_delay
        self.jitter = jitter
        self.sleep = sleep
        self.on_retry = on_retry

    def call(self, operation, function):
        for attempt in range(1, self.attempts + 1):
            try:
                return function()
            except Exception as error:
                if not _transient(error):
                    if isinstance(error, ClientError):
                        raise S3Error(operation + " failed (" + _code(error) + ")") from None
                    if isinstance(error, botocore_errors.SSLError):
                        raise S3Error(operation + " failed (TLS verification/transport)") from None
                    raise
                if attempt == self.attempts:
                    raise RetryExhausted(operation + " exhausted its " + str(attempt) + " attempts") from None
                delay = min(self.maximum_delay,
                            self.initial_delay * 2 ** (attempt - 1) + random.uniform(0, self.jitter))
                event = {"operation": operation, "attempt": attempt,
                         "max_attempts": self.attempts, "delay_seconds": delay,
                         "error_type": type(error).__name__, "code": _code(error)}
                if self.on_retry:
                    self.on_retry(event)
                else:
                    LOG.warning("S3 retry %s", event)
                self.sleep(delay)


def create_client(endpoint=None, profile=None, region="us-east-1"):
    session = botocore.session.Session(profile=profile or None)
    return session.create_client("s3", endpoint_url=endpoint or None, region_name=region,
                                 config=Config(retries={"max_attempts": 0}, connect_timeout=10,
                                               read_timeout=300, s3={"addressing_style": "path"}))


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _expected_source(value):
    """Optional independently reviewed local payload identity, before claiming output."""
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {'size', 'sha256'}
            or type(value['size']) is not int or value['size'] < 0
            or not isinstance(value['sha256'], str) or len(value['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['sha256'])):
        raise ContractError('Malformed independently reviewed upload source identity')


def _stamp(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


class S3Store:
    transport = "s3"

    def __init__(self, output_root=None, client=None, policy=None, endpoint=None, profile=None):
        self.client = client if client is not None else create_client(endpoint, profile)
        retries = self.client.meta.config.retries or {}
        if not (retries.get("total_max_attempts") == 1 or (
                "total_max_attempts" not in retries and retries.get("max_attempts") == 0)):
            raise ValueError("S3Store must own the budget: disable SDK retries")
        if output_root is not None:
            parse_s3(output_root, prefix=True)
        self.output_root = output_root
        self.policy = policy if policy is not None else RetryPolicy()

    def _destination(self, uri):
        if self.output_root is None:
            raise ContractError("A reviewed output prefix is required for mutation")
        assert_within(uri, self.output_root)
        bucket, key = parse_s3(uri)
        return {"Bucket": bucket, "Key": key}

    @contextmanager
    def _conditional(self, operation):
        # Older models lack IfNoneMatch parameters. Add the supported HTTP header
        # before signing instead of replacing/upgrading their scientific runtime.
        event = "before-call.s3." + operation
        unique_id = "modular-create-only-" + uuid.uuid4().hex
        def header(params, **kwargs):
            params["headers"]["If-None-Match"] = "*"
        self.client.meta.events.register_first(event, header, unique_id=unique_id)
        try:
            yield
        finally:
            self.client.meta.events.unregister(event, unique_id=unique_id)

    @staticmethod
    def _arguments(uri, expected=None):
        bucket, key = parse_s3(uri)
        args = {"Bucket": bucket, "Key": key}
        if expected is not None:
            validate_ref(expected)
            if expected["uri"] != uri:
                raise IntegrityError("Object reference URI differs from requested object")
            args["IfMatch"] = '"' + expected["etag"] + '"'
            if "version_id" in expected:
                args["VersionId"] = expected["version_id"]
        return args

    @staticmethod
    def _identity(uri, response):
        return object_ref(uri, response["ContentLength"], response["ETag"],
                          version_id=response.get("VersionId"))

    @staticmethod
    def _match(expected, observed):
        if any(expected.get(k) != observed.get(k) for k in ("uri", "size", "etag")):
            raise IntegrityError("Stored object identity changed")
        if "version_id" in expected and expected["version_id"] != observed.get("version_id"):
            raise IntegrityError("Stored object version changed")

    def _head_once(self, uri, expected=None, optional=False):
        try:
            response = self.client.head_object(**self._arguments(uri, expected))
        except ClientError as error:
            if optional and _missing(error):
                return None
            if _missing(error):
                raise ObjectMissing("Expected object is missing") from None
            raise
        observed = self._identity(uri, response)
        if expected is not None:
            self._match(expected, observed)
        return observed, response.get("Metadata", {})

    def verify(self, ref):
        """Verify the frozen ETag/size/version, not a guessed MD5 or full byte digest."""
        validate_ref(ref)
        self.policy.call("verify_object", lambda: self._head_once(ref["uri"], ref))
        return ref

    def stat(self, uri):
        """Freeze current ETag/size/version before scientific input staging."""
        return self.policy.call("stat_object", lambda: self._head_once(uri))[0]

    def inspect_object(self, ref):
        """Read metadata bound to a frozen identity; HEAD does not verify SHA-256."""
        validate_ref(ref)
        observed, metadata = self.policy.call("inspect_object", lambda: self._head_once(ref['uri'], ref))
        return {'object': observed, 'metadata': dict(metadata)}

    def verify_contents(self, ref):
        """Stream frozen bytes, enforcing any known SHA and returning a full SHA.

        Scientific objects are not JSON metadata. This bounded-memory sink has no
        metadata-size cap and retries the complete signed GET/body operation.
        """
        validate_ref(ref)
        class HashSink:
            def write(self, data):
                pass
        def operation():
            return self._read_once(ref["uri"], expected=ref, limit=None, sink=HashSink())[1]
        return self.policy.call("verify_contents", operation)

    def read_range(self, ref, offset, length):
        """Read one exact bounded range, not proof of the full object's SHA-256."""
        validate_ref(ref)
        if (type(offset) is not int or type(length) is not int or offset < 0
                or not 0 < length <= MAX_RANGE_BYTES or offset + length > ref['size']):
            raise ContractError('Range must be positive, bounded and inside the frozen object')
        end = offset + length - 1
        arguments = dict(self._arguments(ref['uri'], ref), Range='bytes=%d-%d' % (offset, end))
        def operation():
            try:
                response = self.client.get_object(**arguments)
            except _SDK_CHECKSUM_ERRORS:
                raise IntegrityError('Object SDK checksum differs from the frozen reference') from None
            except ClientError as error:
                if _missing(error):
                    raise ObjectMissing('Expected range source is missing') from None
                raise
            body = response.get('Body')
            if body is None:
                raise IntegrityError('Range response has no body')
            try:
                if (response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 206
                        or type(response.get('ContentLength')) is not int
                        or response['ContentLength'] != length
                        or response.get('ContentRange') != 'bytes %d-%d/%d' % (offset, end, ref['size'])):
                    raise IntegrityError('Range response does not match the frozen byte interval')
                try:
                    observed = object_ref(ref['uri'], ref['size'], response.get('ETag'),
                                          version_id=response.get('VersionId'))
                except ContractError:
                    raise IntegrityError('Range response has a malformed object identity') from None
                self._match(ref, observed)
                chunks, count = [], 0
                while True:
                    block = body.read(min(1024 * 1024, length - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > length:
                        raise IntegrityError('Range body exceeds the frozen byte interval')
                    chunks.append(block)
                if count != length:
                    raise botocore_errors.IncompleteReadError(actual_bytes=count, expected_bytes=length)
                return b''.join(chunks)
            except _SDK_CHECKSUM_ERRORS:
                raise IntegrityError('Object SDK checksum differs from the frozen reference') from None
            finally:
                body.close()
        return self.policy.call('read_range', operation)

    def _read_once(self, uri, expected=None, optional=False, limit=MAX_JSON_BYTES, sink=None,
                   exact_size=None):
        try:
            response = self.client.get_object(**self._arguments(uri, expected))
        except _SDK_CHECKSUM_ERRORS:
            raise IntegrityError('Object SDK checksum differs from the frozen reference') from None
        except ClientError as error:
            if optional and _missing(error):
                return None
            if _missing(error):
                raise ObjectMissing("Expected object is missing") from None
            raise
        body = response["Body"]
        try:
            observed = self._identity(uri, response)
            if expected is not None:
                self._match(expected, observed)
            if exact_size is not None and observed["size"] != exact_size:
                raise ImmutableConflict("Existing immutable object has a different size")
            if limit is not None and observed["size"] > limit:
                raise IntegrityError("Announced object exceeds the metadata limit")
            chunks, count, sha256 = [], 0, hashlib.sha256()
            while True:
                block = body.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if limit is not None and count > limit:
                    raise IntegrityError("Streamed object exceeds the metadata limit")
                sha256.update(block)
                if sink is None:
                    chunks.append(block)
                else:
                    sink.write(block)
            if count != observed["size"]:
                raise botocore_errors.IncompleteReadError(actual_bytes=count, expected_bytes=observed["size"])
            observed["sha256"] = sha256.hexdigest()
            if expected and expected.get("sha256", observed["sha256"]) != observed["sha256"]:
                raise IntegrityError("Object SHA-256 differs from the frozen reference")
            return (b"".join(chunks) if sink is None else None), observed
        except _SDK_CHECKSUM_ERRORS:
            raise IntegrityError('Object SDK checksum differs from the frozen reference') from None
        finally:
            body.close()

    def read_json(self, uri, expected=None, optional=False):
        if expected is not None and optional:
            raise ValueError("A frozen object reference must not be treated as optional")
        result = self.policy.call("read_json", lambda: self._read_once(uri, expected, optional))
        if result is None:
            return None
        data, ref = result
        return decode_json(data), ref

    def download(self, ref, destination):
        validate_ref(ref)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        def operation():
            fd, temporary = tempfile.mkstemp(prefix=".modular-download-", dir=str(destination.parent))
            try:
                with os.fdopen(fd, "wb") as output:
                    _, observed = self._read_once(ref["uri"], ref, limit=None, sink=output)
                os.replace(temporary, str(destination))
                return observed
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return self.policy.call("download", operation)

    def _existing_bytes(self, uri, data):
        try:
            observed = self._read_once(uri, optional=True, limit=len(data))
        except IntegrityError:
            raise ImmutableConflict("Existing immutable object has different bytes or length") from None
        if observed is None:
            return None
        if observed[0] != data:
            raise ImmutableConflict("Immutable object already contains different bytes")
        return observed[1]

    def _put_bytes(self, uri, data, content_type="application/octet-stream", metadata=None):
        args = self._destination(uri)
        sha256 = hashlib.sha256(data).hexdigest()
        def operation():
            try:
                with self._conditional("PutObject"):
                    self.client.put_object(**args, Body=data, ContentType=content_type,
                                           ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode(),
                                           Metadata=dict(metadata or {}, sha256=sha256))
            except Exception as error:
                if _code(error) == "PreconditionFailed" or _code(error) == "412" or _transient(error):
                    existing = self._existing_bytes(uri, data)
                    if existing is not None:
                        return existing
                if _code(error) in {"409", "ConditionalRequestConflict"}:
                    raise ConnectionResetError("Conditional create conflicted") from None
                raise
            observed = self._existing_bytes(uri, data)
            if observed is None:
                raise ConnectionResetError("Published object is not yet visible")
            return observed
        return self.policy.call("publish_object", operation)

    def put_json(self, uri, value):
        data = canonical_json(value)
        existing = self._claim(uri, len(data), hashlib.sha256(data).hexdigest())
        return existing if existing is not None else self._put_bytes(uri, data, content_type="application/json")

    def _existing_file(self, uri, size, sha256):
        class HashSink:
            def write(self, data):
                pass
        # _read_once hashes the complete frozen object, with a bounded-memory sink.
        result = self._read_once(uri, optional=True, limit=None, sink=HashSink(), exact_size=size)
        if result is None:
            return None
        _, actual = result
        if actual["sha256"] != sha256:
            raise ImmutableConflict("Existing immutable object has different bytes")
        return actual

    def _claim(self, uri, size, sha256):
        """One permanent small-object intent per destination, across all writers.

        Some deployed S3 services ignore conditional multipart completion. A
        conditional PUT of this never-multipart sidecar serializes our writers
        before any scientific bytes can be published at their destination.
        Incomplete intents are never stolen/restarted by another invocation.
        """
        self._destination(uri)
        if uri.endswith(CLAIM_SUFFIX):
            raise ContractError("Upload-intent filenames are reserved for the transport")
        claim_uri = uri + CLAIM_SUFFIX
        identity = {"schema_version": 1, "target_uri": uri, "size": size, "sha256": sha256}
        claim = dict(identity, nonce=uuid.uuid4().hex)
        try:
            self._put_bytes(claim_uri, canonical_json(claim), content_type="application/json")
        except ImmutableConflict:
            saved, _ = self.read_json(claim_uri)
            if (not isinstance(saved, dict) or set(saved) != set(claim)
                    or any(saved.get(k) != v for k, v in identity.items())):
                raise ImmutableConflict("Destination is claimed for different immutable content") from None
            existing = self.policy.call("verify_completed_output", lambda: self._existing_file(uri, size, sha256))
            if existing is None:
                failure = UploadUncertain("Destination has an incomplete upload intent; use a fresh run/step path")
                failure.intent_uri = claim_uri
                raise failure
            return existing
        # Also protect previously published objects that predate this protocol.
        return self.policy.call("verify_existing_output", lambda: self._existing_file(uri, size, sha256))

    def upload_file(self, uri, source, part_bytes=PART_BYTES, *, expected_source=None):
        """Create-only output; accepted multipart parts are retained across retries."""
        args = self._destination(uri)
        _expected_source(expected_source)
        if type(part_bytes) is not int or part_bytes < MIN_PART_BYTES:
            raise ValueError("Multipart part size must be at least 5 MiB")
        source = Path(source)
        stamp = _stamp(source)
        size = stamp[0]
        if (size + part_bytes - 1) // part_bytes > MAX_PARTS:
            raise ValueError("Upload exceeds 10,000 parts; choose a larger part size")
        sha256 = _sha256_file(source)
        if _stamp(source) != stamp:
            raise IntegrityError("Local source changed while hashing")
        if expected_source is not None and expected_source != {'size': size, 'sha256': sha256}:
            raise IntegrityError('Upload source differs from its independently reviewed identity')
        existing = self._claim(uri, size, sha256)
        if existing is not None:
            return existing
        nonce = uuid.uuid4().hex
        metadata = {"sha256": sha256, "ephys-upload-id": nonce}
        if size < part_bytes:
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise IntegrityError("Local source changed before upload")
            return self._put_bytes(uri, data, metadata=metadata)
        # Initiation has no idempotency key. A lost response cannot safely be
        # handled by starting another session behind the caller's retry budget.
        try:
            upload_id = self.client.create_multipart_upload(**args, Metadata=metadata)["UploadId"]
            if not isinstance(upload_id, str) or not upload_id:
                raise UploadUncertain("Multipart initiation omitted a valid session ID")
        except Exception:
            raise UploadUncertain("Multipart initiation was not acknowledged; no session was restarted") from None
        parts = []
        session_completed = False
        primary_error = None
        try:
            with source.open("rb") as stream:
                number = 0
                while True:
                    data = stream.read(part_bytes)
                    if not data:
                        break
                    number += 1
                    if _stamp(source) != stamp:
                        raise IntegrityError("Local source changed during upload")
                    def part():
                        return self.client.upload_part(**args, UploadId=upload_id, PartNumber=number,
                                                       Body=data, ContentMD5=base64.b64encode(
                                                           hashlib.md5(data).digest()).decode())["ETag"]
                    etag = self.policy.call("upload_part_" + str(number), part)
                    if not etag:
                        raise IntegrityError("Upload part response omitted its ETag")
                    parts.append({"PartNumber": number, "ETag": etag})
            if _stamp(source) != stamp:
                raise IntegrityError("Local source changed before completion")
            def completed_object(allow_foreign=False):
                nonlocal session_completed
                observed = self._head_once(uri, optional=True)
                if observed is None:
                    return None
                ref, stored_metadata = observed
                if ref["size"] != size:
                    raise ImmutableConflict("Completed object has a different size")
                if stored_metadata.get("ephys-upload-id") == nonce and stored_metadata.get("sha256") == sha256:
                    session_completed = True
                    ref["sha256"] = sha256
                    return ref
                if allow_foreign:
                    # Another create-only writer may have published identical
                    # bytes. Stream its exact object instead of trusting metadata.
                    class HashSink:
                        def __init__(self):
                            self.sha256 = hashlib.sha256()
                        def write(self, data):
                            self.sha256.update(data)
                    sink = HashSink()
                    self._read_once(uri, ref, limit=None, sink=sink)
                    if sink.sha256.hexdigest() == sha256:
                        ref["sha256"] = sha256
                        return ref
                raise ImmutableConflict("Completed object does not belong to this upload")
            def complete():
                try:
                    with self._conditional("CompleteMultipartUpload"):
                        self.client.complete_multipart_upload(**args, UploadId=upload_id,
                                                              MultipartUpload={"Parts": parts})
                except Exception as error:
                    if _code(error) in {"412", "PreconditionFailed"}:
                        existing = completed_object(allow_foreign=True)
                        if existing is not None:
                            return existing
                    if _transient(error) or _code(error) == "NoSuchUpload":
                        existing = completed_object()
                        if existing is not None:
                            return existing
                    # A conditional-completion 409 requires a new upload on S3.
                    # That would retransmit accepted parts: leave it terminal.
                    raise
                result = completed_object()
                if result is None:
                    raise ConnectionResetError("Completed object is not yet visible")
                return result
            return self.policy.call("complete_upload", complete)
        except BaseException as error:
            primary_error = error
            error.upload_id = upload_id
            error.object_uri = uri
            raise
        finally:
            # Abort a known session after failure, or a losing identical writer.
            # NoSuchUpload also covers an already committed successful session.
            def abort():
                try:
                    self.client.abort_multipart_upload(**args, UploadId=upload_id)
                except ClientError as error:
                    if _code(error) != "NoSuchUpload":
                        raise
            if not session_completed:
                try:
                    self.policy.call("abort_upload", abort)
                except Exception:
                    LOG.error("Multipart cleanup failed: upload_id=%s; object=%s", upload_id, uri)
                    if primary_error is None:
                        failure = UploadUncertain("Verified existing output, but the losing upload session needs cleanup")
                        failure.upload_id, failure.object_uri = upload_id, uri
                        raise failure from None
                    primary_error.cleanup_failed = True
