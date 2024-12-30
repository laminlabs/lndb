# we are not documenting UPath here because it's documented at lamindb.UPath
"""Paths & file systems."""

from __future__ import annotations

import os
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from functools import partial
from itertools import islice
from pathlib import Path, PosixPath, PurePosixPath, WindowsPath
from typing import TYPE_CHECKING, Any, Literal

import fsspec
from lamin_utils import logger
from upath import UPath
from upath.implementations.cloud import CloudPath, S3Path  # keep CloudPath!
from upath.implementations.local import LocalPath

from ._aws_credentials import HOSTED_BUCKETS, get_aws_credentials_manager
from .hashing import HASH_LENGTH, b16_to_b64, hash_from_hashes_list

if TYPE_CHECKING:
    from .types import UPathStr

LocalPathClasses = (PosixPath, WindowsPath, LocalPath)

# also see https://gist.github.com/securifera/e7eed730cbe1ce43d0c29d7cd2d582f4
#    ".gz" is not listed here as it typically occurs with another suffix
# the complete list is at lamindb.core.storage._suffixes
VALID_SIMPLE_SUFFIXES = {
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
    ".qs",  # https://cran.r-project.org/web/packages/qs/vignettes/vignette.html
    ".rds",
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
# below gets updated within lamindb because it's frequently changing
VALID_COMPOSITE_SUFFIXES = {".anndata.zarr"}

TRAILING_SEP = (os.sep, os.altsep) if os.altsep is not None else os.sep


def extract_suffix_from_path(path: Path, arg_name: str | None = None) -> str:
    def process_digits(suffix: str):
        if suffix[1:].isdigit():  # :1 to skip the dot
            return ""  # digits are no valid suffixes
        else:
            return suffix

    if len(path.suffixes) <= 1:
        return process_digits(path.suffix)

    total_suffix = "".join(path.suffixes)
    if total_suffix in VALID_SIMPLE_SUFFIXES:
        return total_suffix
    elif total_suffix.endswith(tuple(VALID_COMPOSITE_SUFFIXES)):
        # below seems slow but OK for now
        for suffix in VALID_COMPOSITE_SUFFIXES:
            if total_suffix.endswith(suffix):
                break
        return suffix
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
        if path.suffixes[-2] in VALID_SIMPLE_SUFFIXES:
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
                " lamindb.core.storage.VALID_SIMPLE_SUFFIXES.add()"
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


def print_hook(size: int, value: int, objectname: str, action: str):
    if size == 0:
        progress_in_percent = 100.0
    else:
        progress_in_percent = (value / size) * 100
    out = f"... {action} {objectname}:" f" {min(progress_in_percent, 100):4.1f}%"
    if "NBPRJ_TEST_NBPATH" not in os.environ:
        end = "\n" if progress_in_percent >= 100 else "\r"
        print(out, end=end)


class ProgressCallback(fsspec.callbacks.Callback):
    def __init__(
        self,
        objectname: str,
        action: Literal["uploading", "downloading", "synchronizing"],
        adjust_size: bool = False,
    ):
        assert action in {"uploading", "downloading", "synchronizing"}

        super().__init__()

        self.action = action
        print_progress = partial(print_hook, objectname=objectname, action=action)
        self.hooks = {"print_progress": print_progress}

        self.adjust_size = adjust_size

    def absolute_update(self, value):
        pass

    def relative_update(self, inc=1):
        pass

    def update_relative_value(self, inc=1):
        if inc != 0:
            self.value += inc
        # this is specific to http filesystem
        # for some reason the last update is 0 always
        # here 100% is forced manually in this case
        elif self.value >= 0.999:
            self.value = self.size
        self.call()

    def branch(self, path_1, path_2, kwargs):
        if self.adjust_size:
            if Path(path_2 if self.action != "uploading" else path_1).is_dir():
                self.size -= 1
        kwargs["callback"] = ChildProgressCallback(self)

    def branched(self, path_1, path_2, **kwargs):
        self.branch(path_1, path_2, kwargs)
        return kwargs["callback"]

    def wrap(self, iterable):
        if self.adjust_size:
            paths = []
            for lpath, rpath in iterable:
                paths.append((lpath, rpath))
                if Path(lpath).is_dir():
                    self.size -= 1
            self.adjust_size = False
            return paths
        else:
            return iterable

    @classmethod
    def requires_progress(
        cls,
        maybe_callback: fsspec.callbacks.Callback | None,
        print_progress: bool,
        objectname: str,
        action: Literal["uploading", "downloading", "synchronizing"],
        **kwargs,
    ):
        if maybe_callback is None:
            if print_progress:
                return cls(objectname, action, **kwargs)
            else:
                return fsspec.callbacks.NoOpCallback()
        return maybe_callback


class ChildProgressCallback(fsspec.callbacks.Callback):
    def __init__(self, parent: ProgressCallback):
        super().__init__()

        self.parent = parent

    def parent_update(self, inc=1):
        self.parent.update_relative_value(inc)

    def relative_update(self, inc=1):
        if self.size != 0:
            self.parent_update(inc / self.size)
        else:
            self.parent_update(1)


def download_to(self, local_path: UPathStr, print_progress: bool = True, **kwargs):
    """Download from self (a destination in the cloud) to the local path."""
    is_file = kwargs.pop("is_file", None)
    if is_file is None:
        is_file = self.is_file()

    if is_file:
        kwargs.pop("recursive", None)
    elif "recursive" not in kwargs:
        kwargs["recursive"] = True

    if print_progress and "callback" not in kwargs:
        objectname = PurePosixPath(local_path).name
        action: Literal["uploading", "downloading", "synchronizing"] = "downloading"
        if is_file:
            show_progress = partial(print_hook, objectname=objectname, action=action)
            callback = fsspec.callbacks.Callback(hooks={"show_progress": show_progress})
        else:
            callback = ProgressCallback(objectname, action, adjust_size=True)
        kwargs["callback"] = callback

    rpath = str(self)
    lpath = str(local_path)
    # fs.download requires ListObjectsV2 on s3
    # using fs.get_file allows to circumvent this requirement
    if is_file:
        self.fs.get_file(rpath, lpath, **kwargs)
    else:
        self.fs.download(rpath, lpath, **kwargs)


def upload_from(
    self,
    local_path: UPathStr,
    create_folder: bool | None = None,
    print_progress: bool = True,
    **kwargs,
) -> UPath:
    """Upload from the local path to `self` (a destination in the cloud).

    If the local path is a directory, recursively upload its contents.

    Args:
        local_path: A local path of a file or directory.
        create_folder: Only applies if `local_path` is a directory and then
            defaults to `True`. If `True`, make a new folder in the destination
            using the directory name of `local_path`. If `False`, upload the
            contents of the directory to to the root-level of the destination.
        print_progress: Print progress.

    Returns:
        The destination path.
    """
    local_path = Path(local_path)
    local_path_is_dir = local_path.is_dir()
    if create_folder is None:
        create_folder = local_path_is_dir
    if create_folder and not local_path_is_dir:
        raise ValueError("create_folder can only be True if local_path is a directory")

    if print_progress and "callback" not in kwargs:
        callback = ProgressCallback(local_path.name, "uploading")
        kwargs["callback"] = callback

    source: str | list[str]
    destination: str | list[str]
    if local_path_is_dir and not create_folder:
        source = [f.as_posix() for f in local_path.rglob("*") if f.is_file()]
        destination = fsspec.utils.other_paths(
            source, self.as_posix(), exists=False, flatten=False
        )
    else:
        source = local_path.as_posix()
        destination = self.as_posix()

    # the below lines are to avoid s3fs triggering create_bucket in upload if
    # dirs are present it allows to avoid permission error
    # would be easier to just
    if self.protocol == "s3" and local_path_is_dir and create_folder:
        bucket = self.drive
        if bucket not in self.fs.dircache:
            self.fs.dircache[bucket] = [{}]
            assert isinstance(destination, str)
            if not destination.endswith(TRAILING_SEP):  # type: ignore
                destination += "/"
            cleanup_cache = True
        else:
            cleanup_cache = False
    else:
        cleanup_cache = False

    self.fs.upload(source, destination, recursive=create_folder, **kwargs)

    if cleanup_cache:
        # normally this is invalidated after the upload but still better to check
        if bucket in self.fs.dircache:
            del self.fs.dircache[bucket]

    if local_path_is_dir and create_folder:
        return self / local_path.name
    else:
        return self


def synchronize(
    self,
    objectpath: Path,
    error_no_origin: bool = True,
    print_progress: bool = False,
    callback: fsspec.callbacks.Callback | None = None,
    timestamp: float | None = None,
):
    """Sync to a local destination path."""
    protocol = self.protocol
    # optimize the number of network requests
    if timestamp is not None:
        is_dir = False
        exists = True
        cloud_mts = timestamp
    else:
        try:
            cloud_stat = self.stat()
            cloud_info = cloud_stat.as_info()
            exists = True
            is_dir = cloud_info["type"] == "directory"
            if not is_dir:
                # hf requires special treatment
                if protocol == "hf":
                    cloud_mts = cloud_info["last_commit"].date.timestamp()
                else:
                    cloud_mts = cloud_stat.st_mtime
        except FileNotFoundError:
            exists = False

    if not exists:
        warn_or_error = f"The original path {self} does not exist anymore."
        if objectpath.exists():
            warn_or_error += (
                f"\nHowever, the local path {objectpath} still exists, you might want"
                " to reupload the object back."
            )
            logger.warning(warn_or_error)
        elif error_no_origin:
            warn_or_error += "\nIt is not possible to synchronize."
            raise FileNotFoundError(warn_or_error)
        return None

    # synchronization logic for directories
    # to synchronize directories, it should be possible to get modification times
    if is_dir:
        files = self.fs.find(str(self), detail=True)
        if protocol == "s3":
            get_modified = lambda file_stat: file_stat["LastModified"]
        elif protocol == "gs":
            get_modified = lambda file_stat: file_stat["mtime"]
        elif protocol == "hf":
            get_modified = lambda file_stat: file_stat["last_commit"].date
        else:
            raise ValueError(f"Can't synchronize a directory for {protocol}.")
        if objectpath.exists():
            destination_exists = True
            cloud_mts_max = max(
                get_modified(file) for file in files.values()
            ).timestamp()
            local_mts = [
                file.stat().st_mtime for file in objectpath.rglob("*") if file.is_file()
            ]
            n_local_files = len(local_mts)
            local_mts_max = max(local_mts)
            if local_mts_max == cloud_mts_max:
                need_synchronize = n_local_files != len(files)
            elif local_mts_max > cloud_mts_max:
                need_synchronize = False
            else:
                need_synchronize = True
        else:
            destination_exists = False
            need_synchronize = True
        if need_synchronize:
            callback = ProgressCallback.requires_progress(
                callback, print_progress, objectpath.name, "synchronizing"
            )
            callback.set_size(len(files))
            origin_file_keys = []
            for file, stat in callback.wrap(files.items()):
                file_key = PurePosixPath(file).relative_to(self.path).as_posix()
                origin_file_keys.append(file_key)
                timestamp = get_modified(stat).timestamp()
                origin = f"{protocol}://{file}"
                destination = objectpath / file_key
                child = callback.branched(origin, destination.as_posix())
                UPath(origin, **self.storage_options).synchronize(
                    destination, callback=child, timestamp=timestamp
                )
                child.close()
            if destination_exists:
                local_files = [file for file in objectpath.rglob("*") if file.is_file()]
                if len(local_files) > len(files):
                    for file in local_files:
                        if (
                            file.relative_to(objectpath).as_posix()
                            not in origin_file_keys
                        ):
                            file.unlink()
                            parent = file.parent
                            if next(parent.iterdir(), None) is None:
                                parent.rmdir()
        return None

    # synchronization logic for files
    if callback is None:
        if print_progress:
            show_progress = partial(
                print_hook, objectname=objectpath.name, action="synchronizing"
            )
            callback = fsspec.callbacks.Callback(hooks={"show_progress": show_progress})
        else:
            callback = fsspec.callbacks.NoOpCallback()

    if objectpath.exists():
        if cloud_mts != 0:
            local_mts_obj = objectpath.stat().st_mtime
            need_synchronize = cloud_mts > local_mts_obj
        else:
            # this is true for http for example
            # where size is present but st_mtime is not
            # we assume that any change without the change in size is unlikely
            cloud_size = cloud_stat.st_size
            local_size_obj = objectpath.stat().st_size
            need_synchronize = cloud_size != local_size_obj
    else:
        objectpath.parent.mkdir(parents=True, exist_ok=True)
        need_synchronize = True
    if need_synchronize:
        # hf has sync filesystem
        # on sync filesystems ChildProgressCallback.branched()
        # returns the default callback
        # this is why a difference between s3 and hf in progress bars
        self.download_to(
            objectpath, print_progress=False, is_file=True, callback=callback
        )
        if cloud_mts != 0:
            os.utime(objectpath, times=(cloud_mts, cloud_mts))
    else:
        # nothing happens if parent_update is not defined
        # because of Callback.no_op
        callback.parent_update()


def modified(self) -> datetime | None:
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
    n_max_files_per_dir_and_type: int = 100,
    n_max_files: int = 1000,
    include_paths: set[Any] | None = None,
    skip_suffixes: list[str] | None = None,
) -> tuple[str, int]:
    space = "    "
    branch = "│   "
    tee = "├── "
    last = "└── "
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
        n_files_per_dir_and_type = defaultdict(lambda: 0)  # type: ignore
        # TODO: pass strict=False to zip with python > 3.9
        for pointer, child_path in zip(pointers, contents):  # type: ignore
            if child_path.is_dir():
                if include_dirs and child_path not in include_dirs:
                    continue
                yield prefix + pointer + child_path.name + "/"
                n_directories += 1
                n_files_per_dir_and_type = defaultdict(lambda: 0)
                extension = branch if pointer == tee else space
                yield from inner(child_path, prefix=prefix + extension, level=level - 1)
            elif not only_dirs:
                if include_paths and child_path not in include_paths:
                    continue
                suffix = extract_suffix_from_path(child_path)
                suffixes.add(suffix)
                n_files_per_dir_and_type[suffix] += 1
                n_objects += 1
                if n_files_per_dir_and_type[suffix] == n_max_files_per_dir_and_type:
                    yield prefix + "..."
                elif n_files_per_dir_and_type[suffix] > n_max_files_per_dir_and_type:
                    continue
                else:
                    yield prefix + pointer + child_path.name

    folder_tree = ""
    iterator = inner(path, level=level)
    for line in islice(iterator, n_max_files):
        folder_tree += f"\n{line}"
    if next(iterator, None):
        folder_tree += f"\n... only showing {n_max_files} out of {n_objects} files"
    directory_info = "directory" if n_directories == 1 else "directories"
    display_suffixes = ", ".join([f"{suffix!r}" for suffix in suffixes])
    suffix_message = f" with suffixes {display_suffixes}" if n_objects > 0 else ""
    message = (
        f"{n_directories} sub-{directory_info} &"
        f" {n_objects} files{suffix_message}\n{path.resolve()}{folder_tree}"
    )
    return message, n_objects


# adapted from: https://stackoverflow.com/questions/9727673
def view_tree(
    path: Path,
    *,
    level: int = 2,
    only_dirs: bool = False,
    n_max_files_per_dir_and_type: int = 100,
    n_max_files: int = 1000,
    include_paths: set[Any] | None = None,
    skip_suffixes: list[str] | None = None,
) -> None:
    """Print a visual tree structure of files & directories.

    Args:
        level: If `1`, only iterate through one level, if `2` iterate through 2
            levels, if `-1` iterate through entire hierarchy.
        only_dirs: Only iterate through directories.
        n_max_files: Display limit. Will only show this many files. Doesn't affect count.
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
        n_max_files=n_max_files,
        n_max_files_per_dir_and_type=n_max_files_per_dir_and_type,
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
    bucket = upath.drive
    if bucket == "scverse-spatial-eu-central-1":
        region = "eu-central-1"
    elif f"s3://{bucket}" not in HOSTED_BUCKETS:
        response = upath.fs.call_s3("head_bucket", Bucket=bucket)
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

# suppress the warning from upath about hf (huggingface) filesystem
# not being explicitly implemented in upath
warnings.filterwarnings(
    "ignore", module="upath", message=".*'hf' filesystem not explicitly implemented.*"
)


def create_path(path: UPath, access_token: str | None = None) -> UPath:
    path = UPath(path)
    # test whether we have an AWS S3 path
    if not isinstance(path, S3Path):
        return path
    return get_aws_credentials_manager().enrich_path(path, access_token)


def get_stat_file_cloud(stat: dict) -> tuple[int, str | None, str | None]:
    size = stat["size"]
    hash, hash_type = None, None
    # gs, use md5Hash instead of etag for now
    if "md5Hash" in stat:
        # gs hash is already in base64
        hash = stat["md5Hash"].strip('"=')
        hash_type = "md5"
    # hf
    elif "blob_id" in stat:
        hash = b16_to_b64(stat["blob_id"])
        hash_type = "sha1"
    # s3
    # StorageClass is checked to be sure that it is indeed s3
    # because http also has ETag
    elif "ETag" in stat and "StorageClass" in stat:
        etag = stat["ETag"]
        # small files
        if "-" not in etag:
            # only store hash for non-multipart uploads
            # we can't rapidly validate multi-part uploaded files client-side
            # we can add more logic later down-the-road
            hash = b16_to_b64(etag)
            hash_type = "md5"
        else:
            stripped_etag, suffix = etag.split("-")
            suffix = suffix.strip('"')
            hash = b16_to_b64(stripped_etag)
            hash_type = f"md5-{suffix}"  # this is the S3 chunk-hashing strategy
    if hash is not None:
        hash = hash[:HASH_LENGTH]
    return size, hash, hash_type


def get_stat_dir_cloud(path: UPath) -> tuple[int, str | None, str | None, int]:
    objects = path.fs.find(path.as_posix(), detail=True)
    hash, hash_type = None, None
    compute_list_hash = True
    if path.protocol == "s3":
        accessor = "ETag"
    elif path.protocol == "gs":
        accessor = "md5Hash"
    elif path.protocol == "hf":
        accessor = "blob_id"
    else:
        compute_list_hash = False
    sizes = []
    hashes = []
    for object in objects.values():
        sizes.append(object["size"])
        if compute_list_hash:
            hashes.append(object[accessor].strip('"='))
    size = sum(sizes)
    n_objects = len(sizes)
    if compute_list_hash:
        hash, hash_type = hash_from_hashes_list(hashes), "md5-d"
    return size, hash, hash_type, n_objects


class InstanceNotEmpty(Exception):
    pass


# is as fast as boto3: https://lamin.ai/laminlabs/lamindata/transform/krGp3hT1f78N5zKv
def check_storage_is_empty(
    root: UPathStr, *, raise_error: bool = True, account_for_sqlite_file: bool = False
) -> int:
    root_upath = UPath(root)
    root_string = root_upath.as_posix()  # type: ignore
    # we currently touch a 0-byte file in the root of a hosted storage location
    # ({storage_root}/.lamindb/_is_initialized) during storage initialization
    # since path.fs.find raises a PermissionError on empty hosted
    # subdirectories (see lamindb_setup/core/_settings_storage/init_storage).
    n_offset_objects = 1  # because of touched dummy file, see mark_storage_root()
    if root_string.startswith(HOSTED_BUCKETS):
        # in hosted buckets, count across entire root
        directory_string = root_string
        # the SQLite file is not in the ".lamindb" directory
        if account_for_sqlite_file:
            n_offset_objects += 1  # because of SQLite file
    else:
        # in any other storage location, only count in .lamindb
        if not root_string.endswith("/"):
            root_string += "/"
        directory_string = root_string + ".lamindb"
    objects = root_upath.fs.find(directory_string)
    n_objects = len(objects)
    n_diff = n_objects - n_offset_objects
    ask_for_deletion = (
        "delete them prior to deleting the instance"
        if raise_error
        else "consider deleting them"
    )
    message = (
        f"Storage '{directory_string}' contains {n_objects - n_offset_objects} objects"
        f" - {ask_for_deletion}"
    )
    if n_diff > 0:
        if raise_error:
            raise InstanceNotEmpty(message)
        else:
            logger.warning(message)
    return n_diff
