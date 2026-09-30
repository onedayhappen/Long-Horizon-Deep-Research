"""Run ownership-sensitive tests with files owned by the current Windows user."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def windows_file_owner():
    if os.name != "nt":
        yield
        return

    # Elevated CI tokens default to Administrators as the owner of new files.
    # Model a normal user session without bypassing production ownership checks.
    # https://learn.microsoft.com/en-us/windows/win32/secauthz/owner-of-a-new-object
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    advapi.SetTokenInformation.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008 | 0x0080, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())

    def information(kind):
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, kind, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer

    def set_owner(buffer, size):
        if not advapi.SetTokenInformation(token, 4, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        original = information(4)  # TokenOwner: pointer to the previous owner SID.
        user = information(1)  # TokenUser starts with a pointer to the user SID.
        set_owner(user, ctypes.sizeof(ctypes.c_void_p))
        try:
            yield
        finally:
            set_owner(original, ctypes.sizeof(ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)
