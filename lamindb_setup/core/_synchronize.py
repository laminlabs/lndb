from fsspec.asyn import _run_coros_in_chunks
from fsspec.callbacks import _DEFAULT_CALLBACK
from pathlib import Path, PurePosixPath
from lamin_utils import logger
from upath import UPath
import os


PROTOCOL_MODIFIED = {"s3": "LastModified", "gs": "mtime"}


# synchronious version
def synchronize_sync(
    upath: UPath, objectpath: Path, error_no_origin: bool = True, **kwargs
):
    """Sync to a local destination path."""
    # optimize the number of network requests
    if "timestamp" in kwargs:
        is_dir = False
        exists = True
        cloud_mts = kwargs.pop("timestamp")
    else:
        # perform only one network request to check existence, type and timestamp
        try:
            cloud_mts = upath.modified.timestamp()
            is_dir = False
            exists = True
        except FileNotFoundError:
            exists = False
        except IsADirectoryError:
            is_dir = True
            exists = True
            callback = kwargs.pop("callback", _DEFAULT_CALLBACK)

    if not exists:
        warn_or_error = f"The original path {upath} does not exist anymore."
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
    if is_dir:
        files = upath.fs.find(str(upath), detail=True)
        modified_key = PROTOCOL_MODIFIED.get(upath.protocol, None)
        if modified_key is None:
            raise ValueError(f"Can't synchronize a directory for {upath.protocol}.")
        if objectpath.exists():
            destination_exists = True
            cloud_mts_max = max(
                file[modified_key] for file in files.values()
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
            origin_file_keys = []
            callback.set_size(len(files))
            for file, stat in callback.wrap(files.items()):
                destination = PurePosixPath(file).relative_to(upath.path)
                origin_file_keys.append(destination.as_posix())
                timestamp = stat[modified_key].timestamp()
                origin = UPath(f"{upath.protocol}://{file}", **upath._kwargs)
                synchronize_sync(
                    origin, objectpath / destination, timestamp=timestamp, **kwargs
                )
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
    if objectpath.exists():
        local_mts = objectpath.stat().st_mtime  # type: ignore
        need_synchronize = cloud_mts > local_mts
    else:
        objectpath.parent.mkdir(parents=True, exist_ok=True)
        need_synchronize = True
    if need_synchronize:
        upath.download_to(objectpath, **kwargs)
        os.utime(objectpath, times=(cloud_mts, cloud_mts))


# asynchronious version
async def synchronize_async(
    upath: UPath, objectpath: Path, error_no_origin: bool = True, **kwargs
):
    """Sync to a local destination path."""
    modified_key = PROTOCOL_MODIFIED.get(upath.protocol, None)
    if modified_key is None:
        raise ValueError(f"Can't synchronize for {upath.protocol}.")
    kwargs.pop("print_progress", None)
    # optimize the number of network requests
    if "timestamp" in kwargs:
        is_dir = False
        exists = True
        cloud_mts = kwargs.pop("timestamp")
    else:
        try:
            info = await upath.fs._info(str(upath))
            exists = True
            if info["type"] == "directory":
                is_dir = True
                batch_size = kwargs.pop("batch_size", upath.fs.batch_size)
                callback = kwargs.pop("callback", _DEFAULT_CALLBACK)
            else:
                is_dir = False
                cloud_mts = info[modified_key].timestamp()
        except FileNotFoundError:
            exists = False

    if not exists:
        warn_or_error = f"The original path {upath} does not exist anymore."
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
    if is_dir:
        files = await upath.fs._find(str(upath), detail=True)
        if objectpath.exists():
            destination_exists = True
            cloud_mts_max = max(
                file[modified_key] for file in files.values()
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
            origin_file_keys = []
            coros = []
            for file, stat in files.items():
                destination = PurePosixPath(file).relative_to(upath.path)
                origin_file_keys.append(destination.as_posix())
                timestamp = stat[modified_key].timestamp()
                origin = UPath(f"{upath.protocol}://{file}", **upath._kwargs)
                coros.append(
                    synchronize_async(
                        origin, objectpath / destination, timestamp=timestamp, **kwargs
                    )
                )
            callback.set_size(len(files))
            await _run_coros_in_chunks(coros, batch_size=batch_size, callback=callback)
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
    if objectpath.exists():
        local_mts = objectpath.stat().st_mtime  # type: ignore
        need_synchronize = cloud_mts > local_mts
    else:
        objectpath.parent.mkdir(parents=True, exist_ok=True)
        need_synchronize = True
    if need_synchronize:
        await upath.fs._get(str(upath), str(objectpath), **kwargs)
        os.utime(objectpath, times=(cloud_mts, cloud_mts))
