#!/usr/bin/env python2.7

from __future__ import print_function

import os
import hashlib
import math
import binascii

import gevent
import gevent.pool
import gevent.queue
import gevent.monkey

import pprint

gevent.monkey.patch_socket()
gevent.monkey.patch_ssl()
gevent.monkey.patch_os()

from retrying import retry

# the following helper functions are (temporarily) shamelessly stolen from boto.glacier.utils

_MEGABYTE = 1024 * 1024
DEFAULT_PART_SIZE = 4 * _MEGABYTE
MAXIMUM_NUMBER_OF_PARTS = 10000

# This is in USD and is correct for eu-west-1 at the time of writing
# CHECK THIS FOR YOURSELF!
PRICE_PER_THOUSAND_REQUESTS = 0.055
STORAGE_PRICE_PER_GB_MONTH = 0.004
RETRIEVAL_PRICE_PER_THOUSAND_REQUESTS = 0.055
RETRIEVAL_PRICE_PER_GB = 0.01


def tree_hash(fo):
    """
    Given a hash of each 1MB chunk (from chunk_hashes) this will hash
    together adjacent hashes until it ends up with one big one. So a
    tree of hashes.
    """
    hashes = []
    hashes.extend(fo)
    while len(hashes) > 1:
        new_hashes = []
        while True:
            if len(hashes) > 1:
                first = hashes.pop(0)
                second = hashes.pop(0)
                new_hashes.append(hashlib.sha256(first + second).digest())
            elif len(hashes) == 1:
                only = hashes.pop(0)
                new_hashes.append(only)
            else:
                break
        hashes.extend(new_hashes)
    return hashes[0]


def chunk_hashes(bytestring, chunk_size=_MEGABYTE):
    chunk_count = int(math.ceil(len(bytestring) / float(chunk_size)))
    hashes = []
    for i in range(chunk_count):
        start = i * chunk_size
        end = (i + 1) * chunk_size
        hashes.append(hashlib.sha256(bytestring[start:end]).digest())
    if not hashes:
        return [hashlib.sha256(b'').digest()]
    return hashes


def bytes_to_hex(str_as_bytes):
    return binascii.hexlify(str_as_bytes)


def minimum_part_size(size_in_bytes, default_part_size=DEFAULT_PART_SIZE):
    """Calculate the minimum part size needed for a multipart upload.

    Glacier allows a maximum of 10,000 parts per upload.  It also
    states that the maximum archive size is 10,000 * 4 GB, which means
    the part size can range from 1MB to 4GB (provided it is one 1MB
    multiplied by a power of 2).

    This function will compute what the minimum part size must be in
    order to upload a file of size ``size_in_bytes``.

    It will first check if ``default_part_size`` is sufficient for
    a part size given the ``size_in_bytes``.  If this is not the case,
    then the smallest part size than can accomodate a file of size
    ``size_in_bytes`` will be returned.

    If the file size is greater than the maximum allowed archive
    size of 10,000 * 4GB, a ``ValueError`` will be raised.

    """
    # The default part size (4 MB) will be too small for a very large
    # archive, as there is a limit of 10,000 parts in a multipart upload.
    # This puts the maximum allowed archive size with the default part size
    # at 40,000 MB. We need to do a sanity check on the part size, and find
    # one that works if the default is too small.
    part_size = _MEGABYTE
    if (default_part_size * MAXIMUM_NUMBER_OF_PARTS) < size_in_bytes:
        if size_in_bytes > (4096 * _MEGABYTE * 10000):
            raise ValueError("File size too large: %s" % size_in_bytes)
        min_part_size = size_in_bytes / 10000
        power = 3
        while part_size < min_part_size:
            part_size = math.ldexp(_MEGABYTE, power)
            power += 1
        part_size = int(part_size)
    else:
        part_size = default_part_size
    return part_size


# TODO: progress callbacks using basesubscriber

class MultipartUploadPart(object):
    """
    Represent a part - have a part number, the upload, etc.
    self.upload - does what you'd expect
    this should be the first phase in subclassing below to handle S3
    """
    pass


class MultipartPartUploader(gevent.Greenlet):
    def __init__(self, upload, work, callback=None, retries=8):
        gevent.Greenlet.__init__(self)
        self.upload = upload
        self.work = work
        self.retries = retries
        if callback:
            self.link(callback)

    def _run(self):
        filename, offset, size = self.work
        print('Loading chunk %s' % offset)
        chunk = self.readfile(filename, offset, size)
        return self.upload_part(chunk, offset, size)

    def readfile(self, filename, offset, size):
        filesize = os.stat(filename).st_size
        print('Reading bytes %s to %s (or less, if we run out of file to read) of %s' % (offset * size, offset * size + size, filesize))
        with open(filename, 'rb') as fileobj:
            fileobj.seek(offset * size)
            return fileobj.read(size)

    def upload_part(self, chunk, offset, size):
        @retry(stop_max_attempt_number=self.retries)
        def retry_upload(range, checksum, body):
            print('Uploading chunk %s - hashstring %s - range %s' % (offset, checksum, range))
            self.upload.upload_part(range=range, checksum=str(checksum), body=body)

        hashbytes = tree_hash(chunk_hashes(chunk))
        hashstring = bytes_to_hex(hashbytes)
        first_byte = offset * size
        last_byte = first_byte + len(chunk) - 1
        rangestr = 'bytes %d-%d/*' % (first_byte, last_byte)
        retry_upload(rangestr, hashstring, chunk)

        return offset, hashbytes


class GreenGlacierUploader(object):
    class UploadFailedException(Exception):
        pass

    def __init__(self, vault, concurrent_uploads=10, part_size=4194304):
        self.vault = vault
        self.part_size = part_size  # will be overridden on upload
        self.concurrent_uploads = concurrent_uploads

    def prepare(self, filename, description=None):
        """
        Allows you to check the vital stats (including cost) of an upload
        before you commit to it.
        """
        self.filename = filename
        self.description = description or filename
        self.filesize = os.stat(self.filename).st_size
        self.minimum = minimum_part_size(self.filesize)
        self.part_size = max(self.part_size, self.minimum) if self.part_size else self.minimum
        self.total_parts = int((self.filesize / self.part_size) + 1)
        print('Preparing to upload %s with %s %s-sized parts' % (filename, self.total_parts, self.part_size))
        print('This is expected to cost $%s in request fees, transfer is free' % (PRICE_PER_THOUSAND_REQUESTS * self.total_parts / 1000))
        print('Storing this archive will cost $%s per month' % (STORAGE_PRICE_PER_GB_MONTH * self.filesize / 1000000000))
        print('Retrieving this archive will cost $%s in request fees, and $%s in transfer fees' % (RETRIEVAL_PRICE_PER_THOUSAND_REQUESTS / 1000, RETRIEVAL_PRICE_PER_GB * self.filesize / 1000000000))

    def upload(self, filename=None, description=None):
        if filename and filename != self.filename:
            self.prepare(filename, description)
        else:
            self.description = description or self.description
        work_queue = gevent.queue.Queue()
        print('Uploading %s with %s %s-sized parts...' % (self.filename, self.total_parts, self.part_size))
        self.res = [None] * self.total_parts

        multipart_upload = self.vault.initiate_multipart_upload(archiveDescription=self.description,
                                                                partSize=str(self.part_size))
        for part in range(self.total_parts):
            work_queue.put((self.filename, part, self.part_size))

        active = gevent.pool.Pool(self.concurrent_uploads, MultipartPartUploader)
        while not work_queue.empty():  # TODO: replace with list e.g. if work: spawn(m, work.pop())
            work = work_queue.get()
            active.spawn(multipart_upload, work, self.callback)
        active.join()  # wait for final chunks to upload..
        print('Completing uploading with total size %s' % (self.filesize))
        final_checksum = bytes_to_hex(tree_hash(self.res))
        multipart_upload.complete(archiveSize=str(self.filesize), checksum=final_checksum)

    def callback(self, g):
        print('greenlet finished, saving value')
        try:
            part_num, chunk_hash = g.get()
            self.res[part_num] = chunk_hash
        except:
            g.upload.abort()
            raise
