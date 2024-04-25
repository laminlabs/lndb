# we are not documenting UPath here because it's documented at lamindb.UPath
"""Paths & file systems."""

from __future__ import annotations

import os
from datetime import datetime, timezone
import botocore.session
from pathlib import Path
from typing import Literal, Dict
import fsspec
from fsspec.asyn import AsyncFileSystem, sync
from itertools import islice
from typing import Optional, Set, Any, Tuple, List
from collections import defaultdict
from lamin_utils import logger
from upath import UPath
from upath.implementations.cloud import CloudPath, S3Path  # noqa  # keep CloudPath!
from upath.implementations.local import LocalPath, PosixUPath, WindowsUPath
from .types import UPathStr
from .hashing import b16_to_b64, hash_md5s_from_dir
from ._synchronize import synchronize_sync, synchronize_async

LocalPathClasses = (PosixUPath, WindowsUPath, LocalPath)

# also see https://gist.github.com/securifera/e7eed730cbe1ce43d0c29d7cd2d582f4
#    ".gz" is not listed here as it typically occurs with another suffix
# the complete list is at lamindb.core.storage._suffixes
VALID_SUFFIXES = {
    #
    # without readers
    #
    ".fasta",
    ".fastq",
    ".jpg",
    ".mtx",
    ".obo",
    ".pdf",
    ".png",
    ".tar",
    ".tiff",
    ".txt",
    ".tsv",
    ".zip",
    ".xml",
    #
    # with readers (see below)
    #
    ".h5ad",
    ".parquet",
    ".csv",
    ".fcs",
    ".xslx",
    ".zarr",
    ".json",
}


TRAILING_SEP = (os.sep, os.altsep) if os.altsep is not None else os.sep


def extract_suffix_from_path(path: Path, arg_name: Optional[str] = None) -> str:
    def process_digits(suffix: str):
        if suffix[1:].isdigit():  # :1 to skip the dot
            return ""  # digits are no valid suffixes
        else:
            return suffix

    if len(path.suffixes) <= 1:
        return process_digits(path.suffix)

    total_suffix = "".join(path.suffixes)
    if total_suffix in VALID_SUFFIXES:
        return total_suffix
    else:
        print_hint = True
        arg_name = "file" if arg_name is None else arg_name  # for the warning
        msg = f"{arg_name} has more than one suffix (path.suffixes), "
        # first check the 2nd-to-last suffix because it might be followed by .gz
        # or another compression-related suffix
        # Alex thought about adding logic along the lines of path.suffixes[-1]
        # in COMPRESSION_SUFFIXES to detect something like .random.gz and then
        # add ".random.gz" but concluded it's too dangerous it's safer to just
        # use ".gz" in such a case
        if path.suffixes[-2] in VALID_SUFFIXES:
            suffix = "".join(path.suffixes[-2:])
            msg += f"inferring: '{suffix}'"
            # do not print a warning for things like .tar.gz, .fastq.gz
            if path.suffixes[-1] == ".gz":
                print_hint = False
        else:
            suffix = path.suffixes[-1]  # this is equivalent to path.suffix
            msg += (
                f"using only last suffix: '{suffix}' - if you want your composite"
                " suffix to be recognized add it to"
                " lamindb.core.storage.VALID_SUFFIXES.add()"
            )
        if print_hint:
            logger.hint(msg)
        return process_digits(suffix)


def infer_filesystem(path: UPathStr):
    import fsspec  # improve cold start

    path_str = str(path)

    if isinstance(path, UPath):
        fs = path.fs
    else:
        protocol = fsspec.utils.get_protocol(path_str)
        if protocol == "s3":
            fs_kwargs = {"cache_regions": True}
        else:
            fs_kwargs = {}
        fs = fsspec.filesystem(protocol, **fs_kwargs)

    return fs, path_str


# this is needed to avoid CreateBucket permission
class S3FSMap(fsspec.FSMap):
    def __setitem__(self, key, value):
        """Store value in key."""
        key = self._key_to_str(key)
        self.fs.pipe_file(key, fsspec.mapping.maybe_convert(value))


def create_mapper(
    fs,
    url="",
    check=False,
    create=False,
    missing_exceptions=None,
):
    if fsspec.utils.get_protocol(url) == "s3":
        return S3FSMap(
            url, fs, check=check, create=False, missing_exceptions=missing_exceptions
        )
    else:
        return fsspec.FSMap(
            url, fs, check=check, create=create, missing_exceptions=missing_exceptions
        )


def print_hook(size: int, value: int, **kwargs):
    progress_in_percent = (value / size) * 100
    out = (
        f"... {kwargs['action']} {Path(kwargs['filepath']).name}:"
        f" {min(progress_in_percent, 100):4.1f}%"
    )
    if progress_in_percent >= 100:
        out += "\n"
    if "NBPRJ_TEST_NBPATH" not in os.environ:
        print(out, end="\r")


class ProgressCallback(fsspec.callbacks.Callback):
    def __init__(self, action: Literal["uploading", "downloading"]):
        super().__init__()
        self.action = action

    def branch(self, path_1, path_2, kwargs):
        kwargs["callback"] = fsspec.callbacks.Callback(
            hooks=dict(print_hook=print_hook), filepath=path_1, action=self.action
        )

    def call(self, *args, **kwargs):
        return None


def download_to(self, path: UPathStr, print_progress: bool = False, **kwargs):
    """Download to a path."""
    if print_progress:
        # can't do path.is_dir() because path doesn't exist
        # so assume any destination without a suffix is a dir
        # this is temporary until we have a proper progress bar for directories
        if os.path.splitext(path)[-1] not in {"", ".zrad", ".zarr"}:
            cb = ProgressCallback("downloading")
        else:
            # todo: make proper progress bar for directories
            cb = fsspec.callbacks.NoOpCallback()
        kwargs["callback"] = cb
    self.fs.download(str(self), str(path), **kwargs)


def upload_from(
    self,
    path: UPathStr,
    dir_inplace: bool = False,
    print_progress: bool = False,
    **kwargs,
):
    """Upload from a local path."""
    path_is_dir = os.path.isdir(path)
    if not path_is_dir:
        dir_inplace = False

    if print_progress:
        if not path_is_dir:
            cb = ProgressCallback("uploading")
        else:
            # todo: make proper progress bar for directories
            cb = fsspec.callbacks.NoOpCallback()
        kwargs["callback"] = cb

    if dir_inplace:
        path = Path(path)
        source = [f for f in path.rglob("*") if f.is_file()]
        destination = [str(self / f.relative_to(path)) for f in source]
        source = [str(f) for f in source]  # type: ignore
    else:
        source = str(path)  # type: ignore
        destination = str(self)  # type: ignore
    # this weird thing is to avoid s3fs triggering create_bucket in upload
    # if dirs are present
    # it allows to avoid permission error
    if self.protocol != "s3" or not path_is_dir or dir_inplace:
        cleanup_cache = False
    else:
        bucket = self._url.netloc
        if bucket not in self.fs.dircache:
            self.fs.dircache[bucket] = [{}]
            if not destination.endswith(TRAILING_SEP):  # type: ignore
                destination += "/"
            cleanup_cache = True
        else:
            cleanup_cache = False

    self.fs.upload(source, destination, **kwargs)

    if cleanup_cache:
        # normally this is invalidated after the upload but still better to check
        if bucket in self.fs.dircache:
            del self.fs.dircache[bucket]


def synchronize(self, objectpath: Path, error_no_origin: bool = True, **kwargs):
    """Sync to a local destination path."""
    if isinstance(self.fs, AsyncFileSystem):
        sync(
            self.fs.loop, synchronize_async, self, objectpath, error_no_origin, **kwargs
        )
    else:
        synchronize_sync(self, objectpath, error_no_origin, **kwargs)


def modified(self) -> Optional[datetime]:
    """Return modified time stamp."""
    mtime = self.fs.modified(str(self))
    if mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=timezone.utc)
    return mtime.astimezone().replace(tzinfo=None)


def compute_file_tree(
    path: Path,
    *,
    level: int = -1,
    only_dirs: bool = False,
    limit: int = 1000,
    include_paths: Optional[Set[Any]] = None,
    skip_suffixes: Optional[List[str]] = None,
) -> Tuple[str, int]:
    space = "    "
    branch = "│   "
    tee = "├── "
    last = "└── "
    max_files_per_dir_per_type = 7
    if skip_suffixes is None:
        skip_suffixes_tuple = ()
    else:
        skip_suffixes_tuple = tuple(skip_suffixes)  # type: ignore
    n_objects = 0
    n_directories = 0

    # by default only including registered files
    # need a flag and a proper implementation
    suffixes = set()
    include_dirs = set()
    if include_paths is not None:
        include_dirs = {d for p in include_paths for d in p.parents}
    else:
        include_paths = set()

    def inner(dir_path: Path, prefix: str = "", level: int = -1):
        nonlocal n_objects, n_directories, suffixes
        if level == 0:
            return
        stripped_dir_path = dir_path.as_posix().rstrip("/")
        # do not iterate through zarr directories
        if stripped_dir_path.endswith(skip_suffixes_tuple):
            return
        # this is needed so that the passed folder is not listed
        contents = [
            i
            for i in dir_path.iterdir()
            if i.as_posix().rstrip("/") != stripped_dir_path
        ]
        if only_dirs:
            contents = [d for d in contents if d.is_dir()]
        pointers = [tee] * (len(contents) - 1) + [last]
        n_files_per_dir_per_type = defaultdict(lambda: 0)  # type: ignore
        for pointer, child_path in zip(pointers, contents):
            if child_path.is_dir():
                if include_dirs and child_path not in include_dirs:
                    continue
                yield prefix + pointer + child_path.name
                n_directories += 1
                n_files_per_dir_per_type = defaultdict(lambda: 0)
                extension = branch if pointer == tee else space
                yield from inner(child_path, prefix=prefix + extension, level=level - 1)
            elif not only_dirs:
                if include_paths and child_path not in include_paths:
                    continue
                suffix = extract_suffix_from_path(child_path)
                suffixes.add(suffix)
                n_files_per_dir_per_type[suffix] += 1
                n_objects += 1
                if n_files_per_dir_per_type[suffix] == max_files_per_dir_per_type:
                    yield prefix + "..."
                elif n_files_per_dir_per_type[suffix] > max_files_per_dir_per_type:
                    continue
                else:
                    yield prefix + pointer + child_path.name

    folder_tree = ""
    iterator = inner(path, level=level)
    for line in islice(iterator, limit):
        folder_tree += f"\n{line}"
    if next(iterator, None):
        folder_tree += f"\n... only showing {limit} out of {n_objects} files"
    directory_info = "directory" if n_directories == 1 else "directories"
    display_suffixes = ", ".join([f"{suffix!r}" for suffix in suffixes])
    suffix_message = f" with suffixes {display_suffixes}" if n_objects > 0 else ""
    message = (
        f"{path.name} ({n_directories} sub-{directory_info} &"
        f" {n_objects} files{suffix_message}): {folder_tree}"
    )
    return message, n_objects


# adapted from: https://stackoverflow.com/questions/9727673
def view_tree(
    path: Path,
    *,
    level: int = -1,
    only_dirs: bool = False,
    limit: int = 1000,
    include_paths: Optional[Set[Any]] = None,
    skip_suffixes: Optional[List[str]] = None,
) -> None:
    """Print a visual tree structure of files & directories.

    Args:
        level: If `1`, only iterate through one level, if `2` iterate through 2
            levels, if `-1` iterate through entire hierarchy.
        only_dirs: Only iterate through directories.
        limit: Display limit. Will only show this many files. Doesn't affect count.
        include_paths: Restrict to these paths.
        skip_suffixes: Skip directories with these suffixes.

    Examples:
        >>> dir_path = ln.core.datasets.generate_cell_ranger_files(
        >>>     "sample_001", ln.settings.storage
        >>> )
        >>> ln.UPath(dir_path).view_tree()
        3 subdirectories, 15 files
        sample_001
        ├── web_summary.html
        ├── metrics_summary.csv
        ├── molecule_info.h5
        ├── filtered_feature_bc_matrix
        │   ├── features.tsv.gz
        │   ├── barcodes.tsv.gz
        │   └── matrix.mtx.gz
        ├── analysis
        │   └── analysis.csv
        ├── raw_feature_bc_matrix
        │   ├── features.tsv.gz
        │   ├── barcodes.tsv.gz
        │   └── matrix.mtx.gz
        ├── possorted_genome_bam.bam.bai
        ├── cloupe.cloupe
        ├── possorted_genome_bam.bam
        ├── filtered_feature_bc_matrix.h5
        └── raw_feature_bc_matrix.h5
    """
    message, _ = compute_file_tree(
        path,
        level=level,
        only_dirs=only_dirs,
        limit=limit,
        include_paths=include_paths,
        skip_suffixes=skip_suffixes,
    )
    logger.print(message)


def to_url(upath):
    """Public storage URL.

    Generates a public URL for an object in an S3 bucket using fsspec's UPath,
    considering the bucket's region.

    Args:
    - upath: A UPath object representing an S3 path.

    Returns:
    - A string containing the public URL to the S3 object.
    """
    if upath.protocol != "s3":
        raise ValueError("The provided UPath must be an S3 path.")
    key = "/".join(upath.parts[1:])
    bucket = upath._url.netloc
    if bucket == "scverse-spatial-eu-central-1":
        region = "eu-central-1"
    elif f"s3://{bucket}" not in hosted_buckets:
        response = upath.fs.call_s3("head_bucket", Bucket=upath._url.netloc)
        headers = response["ResponseMetadata"]["HTTPHeaders"]
        region = headers.get("x-amz-bucket-region")
    else:
        region = bucket.replace("lamin_", "")
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    else:
        return f"https://{bucket}.s3-{region}.amazonaws.com/{key}"


# Why aren't we subclassing?
#
# The problem is that UPath defines a type system of paths
# Its __new__ method returns instances of different subclasses rather than a
# UPath object
# If we create a custom subclass naively, subclasses of the parent UPath won't
# be subclasses of our custom subclass
# This makes life really hard in type checks involving local to cloud
# comparisons, etc.
# Hence, we extend the existing UPath and amend the docs
# Some of this might end up in the original UPath implementation over time,
# we'll see.


# add custom functions
UPath.modified = property(modified)
UPath.synchronize = synchronize
UPath.upload_from = upload_from
UPath.to_url = to_url
UPath.download_to = download_to
UPath.view_tree = view_tree
# unfortunately, we also have to do this for the subclasses
Path.view_tree = view_tree  # type: ignore

UPath.glob.__doc__ = Path.glob.__doc__
UPath.rglob.__doc__ = Path.rglob.__doc__
UPath.stat.__doc__ = Path.stat.__doc__
UPath.iterdir.__doc__ = Path.iterdir.__doc__
UPath.resolve.__doc__ = Path.resolve.__doc__
UPath.relative_to.__doc__ = Path.relative_to.__doc__
UPath.exists.__doc__ = Path.exists.__doc__
UPath.is_dir.__doc__ = Path.is_dir.__doc__
UPath.is_file.__doc__ = Path.is_file.__doc__
UPath.unlink.__doc__ = Path.unlink.__doc__
UPath.rename.__doc__ = """Move file, see fsspec.AbstractFileSystem.mv.

>>> upath = Upath("s3://my-bucket/my-file")
>>> upath.rename(UPath("s3://my-bucket/my-file-renamed"))
>>> upath.rename("my-file-renamed")

>>> upath = Upath("local-folder/my-file")
>>> upath.rename("local-folder/my-file-renamed")
"""
UPath.__doc__ = """Paths: low-level key-value access to files/objects.

Paths are based on keys that offer the typical access patterns of file systems
 and object stores.

>>> upath = UPath("s3://my-bucket/my-folder")
>>> upath.exists()

Args:
    pathlike: A string or Path to a local/cloud file/directory/folder.
"""


def convert_pathlike(pathlike: UPathStr) -> UPath:
    """Convert pathlike to Path or UPath inheriting options from root."""
    if isinstance(pathlike, (str, UPath)):
        path = UPath(pathlike)
    elif isinstance(pathlike, Path):
        path = UPath(str(pathlike))  # UPath applied on Path gives Path back
    else:
        raise ValueError("pathlike should be of type UPathStr")
    # remove trailing slash
    if path._parts and path._parts[-1] == "":
        path._parts = path._parts[:-1]
    return path


hosted_regions = [
    "eu-central-1",
    "eu-west-2",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
]
lamin_env = os.getenv("LAMIN_ENV")
if lamin_env is None or lamin_env == "prod":
    hosted_buckets_list = [f"s3://lamin-{region}" for region in hosted_regions]
    hosted_buckets_list.append("s3://scverse-spatial-eu-central-1")
    hosted_buckets = tuple(hosted_buckets_list)
else:
    hosted_buckets = ("s3://lamin-hosted-test",)  # type: ignore
credentials_cache: Dict[str, Dict[str, str]] = {}
AWS_CREDENTIALS_PRESENT = None


def create_path(path: UPath, access_token: Optional[str] = None) -> UPath:
    path = convert_pathlike(path)
    # test whether we have an AWS S3 path
    if not isinstance(path, S3Path):
        return path
    # test whether AWS credentials are present
    global AWS_CREDENTIALS_PRESENT

    if AWS_CREDENTIALS_PRESENT is not None:
        anon = AWS_CREDENTIALS_PRESENT
    else:
        if path.fs.key is not None and path.fs.secret is not None:
            anon = False
        else:
            # we could do path.fs.connect()
            # and check path.fs.session._credentials, but it'd be slower
            session = botocore.session.get_session()
            credentials = session.get_credentials()
            if credentials is None or credentials.access_key is None:
                anon = True
            else:
                anon = False
            # cache credentials and reuse further
            AWS_CREDENTIALS_PRESENT = anon

    # test whether we are on hosted storage or not
    path_str = path.as_posix()
    is_hosted_storage = path_str.startswith(hosted_buckets)

    if not is_hosted_storage:
        # make anon request if no credentials present
        return UPath(path, cache_regions=True, anon=anon)

    root_folder = "/".join(path_str.replace("s3://", "").split("/")[:2])

    if access_token is None and root_folder in credentials_cache:
        credentials = credentials_cache[root_folder]
    else:
        from ._hub_core import access_aws

        credentials = access_aws(
            storage_root=f"s3://{root_folder}", access_token=access_token
        )
        if access_token is None:
            credentials_cache[root_folder] = credentials

    return UPath(
        path,
        key=credentials["key"],
        secret=credentials["secret"],
        token=credentials["token"],
    )


def get_stat_file_cloud(stat: Dict) -> Tuple[int, str, str]:
    size = stat["size"]
    # small files
    if "-" not in stat["ETag"]:
        # only store hash for non-multipart uploads
        # we can't rapidly validate multi-part uploaded files client-side
        # we can add more logic later down-the-road
        hash = b16_to_b64(stat["ETag"])
        hash_type = "md5"
    else:
        stripped_etag, suffix = stat["ETag"].split("-")
        suffix = suffix.strip('"')
        hash = f"{b16_to_b64(stripped_etag)}-{suffix}"
        hash_type = "md5-n"  # this is the S3 chunk-hashing strategy
    return size, hash, hash_type


def get_stat_dir_cloud(path: UPath) -> Tuple[int, str, str, int]:
    sizes = []
    md5s = []
    objects = path.fs.find(path.as_posix(), detail=True)
    if path.protocol == "s3":
        accessor = "ETag"
    elif path.protocol == "gs":
        accessor = "md5Hash"
    for object in objects.values():
        sizes.append(object["size"])
        md5s.append(object[accessor].strip('"='))
    size = sum(sizes)
    hash, hash_type = hash_md5s_from_dir(md5s)
    n_objects = len(md5s)
    return size, hash, hash_type, n_objects


class InstanceNotEmpty(Exception):
    pass


# is as fast as boto3: https://lamin.ai/laminlabs/lamindata/transform/krGp3hT1f78N5zKv
def check_storage_is_empty(
    root: UPathStr, *, raise_error: bool = True, account_for_sqlite_file: bool = False
) -> int:
    root_upath = convert_pathlike(root)
    root_string = root_upath.as_posix()  # type: ignore
    # we currently touch a 0-byte file in the root of a hosted storage location
    # ({storage_root}/.lamindb/_is_initialized) during storage initialization
    # since path.fs.find raises a PermissionError on empty hosted
    # subdirectories (see lamindb_setup/core/_settings_storage/init_storage).
    n_offset_objects = 1  # because of touched dummy file, see mark_storage_root()
    if account_for_sqlite_file:
        n_offset_objects += 1  # because of SQLite file
    objects = root_upath.fs.find(root_string)
    n_objects = len(objects)
    n_diff = n_objects - n_offset_objects
    ask_for_deletion = (
        "delete them prior to deleting the instance"
        if raise_error
        else "consider deleting them"
    )
    message = (
        f"Storage location contains {n_objects} objects "
        f"({n_offset_objects} ignored) - {ask_for_deletion}\n{objects}"
    )
    if n_diff > 0:
        if raise_error:
            raise InstanceNotEmpty(message)
        else:
            logger.warning(message)
    return n_diff
