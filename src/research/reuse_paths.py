"""Local, same-owner authorization. Never follow imported paths or reparse points."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from functools import lru_cache
from pathlib import Path


class ReuseError(ValueError):
    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@lru_cache(maxsize=1)
def principal():
    if os.name != "nt":
        return f"uid:{os.getuid()}"
    value = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True, encoding="utf-8")
    match = re.search(r"S-1-[0-9-]+", value)
    if not match:
        raise ReuseError("reuse_source_unverified", "cannot read Windows SID")
    return match[0]


def safe_path(path: Path, root: Path, *, exists=True):
    if '..' in Path(path).parts:
        raise ReuseError('access_restricted', 'path traversal')
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if str(path).startswith(("\\\\", "//")) or ".." in path.parts:
        raise ReuseError("access_restricted", "network/device/traversal path")
    try:
        path.relative_to(root)
    except ValueError:
        raise ReuseError("access_restricted", "source is outside runs-root") from None
    for part in [path, *path.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if part == path and not exists:
                continue
            raise ReuseError("lineage_unavailable", str(part)) from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReuseError("access_restricted", "symlink/junction/reparse point")
        if part == root:
            break
    if exists and not os.access(path, os.R_OK):
        raise ReuseError("access_restricted", "file is not readable")
    return path.resolve()


def checked_blob_path(run_dir, sha, root):
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ReuseError("integrity_error", "invalid blob hash")
    return safe_path(Path(run_dir) / "blobs" / sha, root)


def file_owner(path):
    if os.name != 'nt':
        return f'uid:{Path(path).stat().st_uid}'
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_security = advapi.GetNamedSecurityInfoW
    get_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_security.restype = wintypes.DWORD
    convert = advapi.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    owner_sid, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    result = get_security(str(path), 1, 1, ctypes.byref(owner_sid), None, None, None, ctypes.byref(descriptor))
    if result:
        raise ReuseError('access_restricted', f'cannot verify file owner: {result}')
    value = wintypes.LPWSTR()
    try:
        if not convert(owner_sid, ctypes.byref(value)):
            raise ReuseError('access_restricted', 'cannot decode owner SID')
        return value.value
    finally:
        if value:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))
        kernel.LocalFree(descriptor)


def verify_identity(store, root, *, target_instance=None):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_identity'").fetchone():
        raise ReuseError('reuse_source_unverified', 'source predates recorded creation identity')
    row = store.db.execute("SELECT * FROM run_identity").fetchone()
    if row is None:
        raise ReuseError("reuse_source_unverified", "source predates recorded creation identity")
    if row["owner"] != principal() or Path(row["runs_root"]).resolve() != Path(root).resolve():
        raise ReuseError("access_restricted", "source owner or runs-root differs")
    if row["run_instance_id"] == target_instance:
        raise ReuseError("access_restricted", "source and target have the same identity")
    safe_path(store.run_dir, root)
    safe_path(store.db_path, root)
    if file_owner(store.run_dir) != principal() or file_owner(store.db_path) != principal():
        raise ReuseError('access_restricted', 'OS ownership differs from current principal')
    return dict(row)
