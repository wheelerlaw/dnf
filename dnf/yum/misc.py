# misc.py
# Copyright (C) 2012-2016 Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

"""
Assorted utility functions for yum.
"""

from __future__ import print_function, absolute_import
from __future__ import unicode_literals
from dnf.pycomp import base64_decodebytes, basestring, unicode
from stat import *
import libdnf.utils
import dnf.const
import dnf.crypto
import dnf.exceptions
import dnf.i18n
import errno
import glob
import io
import os
import os.path
import pwd
import re
import shutil
import tempfile

_default_checksums = ['sha256']


_re_compiled_glob_match = None
def re_glob(s):
    """ Tests if a string is a shell wildcard. """
    global _re_compiled_glob_match
    if _re_compiled_glob_match is None:
        _re_compiled_glob_match = re.compile(r'[*?]|\[.+\]').search
    return _re_compiled_glob_match(s)

_re_compiled_full_match = None
def re_full_search_needed(s):
    """ Tests if a string needs a full nevra match, instead of just name. """
    global _re_compiled_full_match
    if _re_compiled_full_match is None:
        # A glob, or a "." or "-" separator, followed by something (the ".")
        one = re.compile(r'.*([-.*?]|\[.+\]).').match
        # Any epoch, for envra
        two = re.compile('[0-9]+:').match
        _re_compiled_full_match = (one, two)
    for rec in _re_compiled_full_match:
        if rec(s):
            return True
    return False

def get_default_chksum_type():
    return _default_checksums[0]

class GenericHolder(object):
    """Generic Holder class used to hold other objects of known types
       It exists purely to be able to do object.somestuff, object.someotherstuff
       or object[key] and pass object to another function that will
       understand it"""

    def __init__(self, iter=None):
        self.__iter = iter

    def __iter__(self):
        if self.__iter is not None:
            return iter(self[self.__iter])

    def __getitem__(self, item):
        if hasattr(self, item):
            return getattr(self, item)
        else:
            raise KeyError(item)

    def all_lists(self):
        """Return a dictionary of all lists."""
        return {key: list_ for key, list_ in vars(self).items()
                if type(list_) is list}

    def merge_lists(self, other):
        """ Concatenate the list attributes from 'other' to ours. """
        for (key, val) in other.all_lists().items():
            vars(self).setdefault(key, []).extend(val)
        return self

def procgpgkey(rawkey):
    '''Convert ASCII-armored GPG key to binary
    '''

    # Normalise newlines
    rawkey = re.sub(b'\r\n?', b'\n', rawkey)

    # Extract block
    block = io.BytesIO()
    inblock = 0
    pastheaders = 0
    for line in rawkey.split(b'\n'):
        if line.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
            inblock = 1
        elif inblock and line.strip() == b'':
            pastheaders = 1
        elif inblock and line.startswith(b'-----END PGP PUBLIC KEY BLOCK-----'):
            # Hit the end of the block, get out
            break
        elif pastheaders and line.startswith(b'='):
            # Hit the CRC line, don't include this and stop
            break
        elif pastheaders:
            block.write(line + b'\n')

    # Decode and return
    return base64_decodebytes(block.getvalue())


def keyInstalled(ts, keyid, timestamp):
    '''
    Return if the GPG key described by the given keyid and timestamp are
    installed in the rpmdb.

    The keyid and timestamp should both be passed as integers.
    The ts is an rpm transaction set object

    Return values:
        - -1      key is not installed
        - 0       key with matching ID and timestamp is installed
        - 1       key with matching ID is installed but has an older timestamp
        - 2       key with matching ID is installed but has a newer timestamp

    No effort is made to handle duplicates. The first matching keyid is used to
    calculate the return result.
    '''
    # Search
    for hdr in ts.dbMatch('name', 'gpg-pubkey'):
        if hdr['version'] == keyid:
            installedts = int(hdr['release'], 16)
            if installedts == timestamp:
                return 0
            elif installedts < timestamp:
                return 1
            else:
                return 2

    return -1


def import_key_to_pubring(rawkey, keyid, gpgdir=None, make_ro_copy=True):
    if not os.path.exists(gpgdir):
        os.makedirs(gpgdir)

    with dnf.crypto.pubring_dir(gpgdir), dnf.crypto.Context() as ctx:
        # import the key
        with open(os.path.join(gpgdir, 'gpg.conf'), 'wb') as fp:
            fp.write(b'')
        ctx.op_import(rawkey)

        if make_ro_copy:

            rodir = gpgdir + '-ro'
            if not os.path.exists(rodir):
                os.makedirs(rodir, mode=0o755)
                for f in glob.glob(gpgdir + '/*'):
                    basename = os.path.basename(f)
                    ro_f = rodir + '/' + basename
                    shutil.copy(f, ro_f)
                    os.chmod(ro_f, 0o755)
                # yes it is this stupid, why do you ask?
                opts = """lock-never
    no-auto-check-trustdb
    trust-model direct
    no-expensive-trust-checks
    no-permission-warning
    preserve-permissions
    """
                with open(os.path.join(rodir, 'gpg.conf'), 'w', 0o755) as fp:
                    fp.write(opts)


        return True


def getCacheDir():
    """return a path to a valid and safe cachedir - only used when not running
       as root or when --tempcache is set"""

    uid = os.geteuid()
    try:
        usertup = pwd.getpwuid(uid)
        username = dnf.i18n.ucd(usertup[0])
        prefix = '%s-%s-' % (dnf.const.PREFIX, username)
    except KeyError:
        prefix = '%s-%s-' % (dnf.const.PREFIX, uid)

    # check for /var/tmp/prefix-* -
    dirpath = '%s/%s*' % (dnf.const.TMPDIR, prefix)
    cachedirs = sorted(glob.glob(dirpath))
    for thisdir in cachedirs:
        stats = os.lstat(thisdir)
        if S_ISDIR(stats[0]) and S_IMODE(stats[0]) == 448 and stats[4] == uid:
            return thisdir

    # make the dir (tempfile.mkdtemp())
    cachedir = tempfile.mkdtemp(prefix=prefix, dir=dnf.const.TMPDIR)
    return cachedir

def seq_max_split(seq, max_entries):
    """ Given a seq, split into a list of lists of length max_entries each. """
    ret = []
    num = len(seq)
    seq = list(seq) # Trying to use a set/etc. here is bad
    beg = 0
    while num > max_entries:
        end = beg + max_entries
        ret.append(seq[beg:end])
        beg += max_entries
        num -= max_entries
    ret.append(seq[beg:])
    return ret

def unlink_f(filename):
    """ Call os.unlink, but don't die if the file isn't there. This is the main
        difference between "rm -f" and plain "rm". """
    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def stat_f(filename, ignore_EACCES=False):
    """ Call os.stat(), but don't die if the file isn't there. Returns None. """
    try:
        return os.stat(filename)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        if ignore_EACCES and e.errno == errno.EACCES:
            return None
        raise

def _getloginuid():
    """ Get the audit-uid/login-uid, if available. os.getuid() is returned
        instead if there was a problem. Note that no caching is done here. """
    #  We might normally call audit.audit_getloginuid(), except that requires
    # importing all of the audit module. And it doesn't work anyway: BZ 518721
    try:
        with open("/proc/self/loginuid") as fo:
            data = fo.read()
            return int(data)
    except (IOError, ValueError):
        return os.getuid()

_cached_getloginuid = None
def getloginuid():
    """ Get the audit-uid/login-uid, if available. os.getuid() is returned
        instead if there was a problem. The value is cached, so you don't
        have to save it. """
    global _cached_getloginuid
    if _cached_getloginuid is None:
        _cached_getloginuid = _getloginuid()
    return _cached_getloginuid


def decompress(filename, dest=None, check_timestamps=False):
    """take a filename and decompress it into the same relative location.
       When the compression type is not recognized (or file is not compressed),
       the content of the file is copied to the destination"""

    if dest:
        out = dest
    else:
        out = None
        dot_pos = filename.rfind('.')
        if dot_pos > 0:
            ext = filename[dot_pos:]
            if ext in ('.zck', '.xz', '.bz2', '.gz', '.lzma', '.zst'):
                out = filename[:dot_pos]
        if out is None:
            raise dnf.exceptions.MiscError("Could not determine destination filename")

    if check_timestamps:
        fi = stat_f(filename)
        fo = stat_f(out)
        if fi and fo and fo.st_mtime == fi.st_mtime:
            return out

    try:
        # libdnf.utils.decompress either decompress file to the destination or
        # copy the content if the compression type is not recognized
        libdnf.utils.decompress(filename, out, 0o644)
    except RuntimeError as e:
        raise dnf.exceptions.MiscError(str(e))

    if check_timestamps and fi:
        os.utime(out, (fi.st_mtime, fi.st_mtime))

    return out


def read_in_items_from_dot_dir(thisglob, line_as_list=True):
    """ Takes a glob of a dir (like /etc/foo.d/\\*.foo) returns a list of all
       the lines in all the files matching that glob, ignores comments and blank
       lines, optional paramater 'line_as_list tells whether to treat each line
       as a space or comma-separated list, defaults to True.
    """
    results = []
    for fname in glob.glob(thisglob):
        with open(fname) as f:
            for line in f:
                if re.match(r'\s*(#|$)', line):
                    continue
                line = line.rstrip() # no more trailing \n's
                line = line.lstrip() # be nice
                if not line:
                    continue
                if line_as_list:
                    line = line.replace('\n', ' ')
                    line = line.replace(',', ' ')
                    results.extend(line.split())
                    continue
                results.append(line)
    return results
