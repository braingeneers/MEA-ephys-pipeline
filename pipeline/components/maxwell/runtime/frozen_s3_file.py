"""Bounded read-only seeking of one frozen S3 object for HDF5 header inspection."""
from collections import OrderedDict
import copy
import io
import os
import traceback

from ephys_modular.contracts import ContractError, validate_ref
from ephys_modular.s3 import MAX_RANGE_BYTES


class HeaderBudgetExceeded(ContractError):
    pass


class FrozenS3File(io.RawIOBase):
    """Cache fixed-size pages; never stage the scientific input or own retries."""

    def __init__(self, store, reference, *, page_bytes=64 * 1024,
                 cache_bytes=8 * 1024 * 1024, max_fetch_bytes=64 * 1024 * 1024,
                 max_requests=2048):
        super().__init__()
        self.pages, self.cached_bytes = OrderedDict(), 0
        validate_ref(reference)
        for value in (page_bytes, cache_bytes, max_fetch_bytes, max_requests):
            if type(value) is not int or value <= 0:
                raise ContractError('Header read budgets must be positive integers')
        if page_bytes > MAX_RANGE_BYTES or cache_bytes < page_bytes:
            raise ContractError('Header cache pages exceed the transport/cache capacity')
        self.store, self.reference = store, copy.deepcopy(reference)
        self.page_bytes, self.cache_bytes = page_bytes, cache_bytes
        self.max_fetch_bytes, self.max_requests = max_fetch_bytes, max_requests
        self.position, self.cached_bytes = 0, 0
        self.pages = OrderedDict()
        self.requests = self.fetched_bytes = self.cache_hits = self.peak_cached_bytes = 0
        self.failure = None

    def readable(self):
        self._checkClosed()
        return True

    def seekable(self):
        self._checkClosed()
        return True

    def tell(self):
        self._checkClosed()
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        self._checkClosed()
        if type(offset) is not int or type(whence) is not int:
            raise TypeError('Seek arguments must be integers')
        if whence not in (os.SEEK_SET, os.SEEK_CUR, os.SEEK_END):
            raise ValueError('Unknown seek origin')
        position = offset + (self.position if whence == os.SEEK_CUR else
                             self.reference['size'] if whence == os.SEEK_END else 0)
        if position < 0:
            raise ValueError('Negative seek position')
        self.position = position
        return position

    def _page(self, start):
        if start in self.pages:
            self.cache_hits += 1
            self.pages.move_to_end(start)
            return self.pages[start]
        length = min(self.page_bytes, self.reference['size'] - start)
        if self.requests + 1 > self.max_requests or self.fetched_bytes + length > self.max_fetch_bytes:
            raise HeaderBudgetExceeded('HDF5 header read budget exhausted; inspect with an explicit larger budget')
        # Counts are logical range operations. S3Store alone owns the bounded
        # retries of each operation, including its complete response body.
        self.requests += 1
        self.fetched_bytes += length
        data = self.store.read_range(self.reference, start, length)
        if not isinstance(data, bytes) or len(data) != length:
            raise ContractError('Header range reader returned an incomplete page')
        while self.cached_bytes + length > self.cache_bytes:
            _, removed = self.pages.popitem(last=False)
            self.cached_bytes -= len(removed)
        self.pages[start] = data
        self.cached_bytes += length
        self.peak_cached_bytes = max(self.peak_cached_bytes, self.cached_bytes)
        return data

    def read(self, size=-1):
        self._checkClosed()
        if type(size) is not int or size < -1:
            raise ValueError('Read size must be a nonnegative integer or -1')
        length = max(0, self.reference['size'] - self.position)
        if size >= 0:
            length = min(length, size)
        output = bytearray()
        try:
            if length > self.max_fetch_bytes:
                raise HeaderBudgetExceeded('A single header read exceeds the explicit byte budget')
            while len(output) < length:
                start = self.position // self.page_bytes * self.page_bytes
                page = self._page(start)
                offset = self.position - start
                block = page[offset:offset + length - len(output)]
                output.extend(block)
                self.position += len(block)
        except Exception as error:
            self.failure = error
            raise
        return bytes(output)

    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)

    def write(self, data):
        self._checkClosed()
        raise io.UnsupportedOperation('Frozen S3 header input is read-only')

    def metrics(self):
        return {'range_operations': self.requests, 'fetched_bytes': self.fetched_bytes,
                'cache_hits': self.cache_hits, 'peak_cache_bytes': self.peak_cached_bytes,
                'page_bytes': self.page_bytes, 'cache_limit_bytes': self.cache_bytes,
                'fetch_limit_bytes': self.max_fetch_bytes, 'operation_limit': self.max_requests}

    def release_error_frames(self, error):
        """Release native references after HDF5 unwinds, before this stream closes.

        A failed h5py constructor can survive in a callback exception's traceback.
        Retaining its file-object driver until interpreter shutdown can then close
        native objects after their backing Python stream. Clear inactive frame
        locals in both the callback and wrapped exception chains while this
        stream is still valid. Keep the original errors, chains and traceback
        locations; clear_frames leaves currently executing frames alone.
        """
        pending, seen = [error, self.failure], set()
        self.failure = None
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            pending.extend((current.__cause__, current.__context__))
            traceback.clear_frames(current.__traceback__)

    def close(self):
        self.pages.clear()
        self.cached_bytes = 0
        super().close()
