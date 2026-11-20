"""Make a big test file without writing its bytes to disk.

`f.truncate(size)` is NOT sparse on Windows (Python's truncate writes every
zero byte there): the model-size tests used to write ~25 GB per suite run and
took 10-110 s each. Marking the file sparse and writing only its last byte
makes it instant, and the code under test still measures a real file of that
size.
"""
import os


def make_sparse_file(path, size: int) -> None:
    with open(path, "wb") as f:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            FSCTL_SET_SPARSE = 0x000900C4
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.DeviceIoControl.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID]
            returned = wintypes.DWORD()
            ok = kernel32.DeviceIoControl(msvcrt.get_osfhandle(f.fileno()), FSCTL_SET_SPARSE,
                                          None, 0, None, 0, ctypes.byref(returned), None)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        if size:
            f.seek(size - 1)      # not truncate(): on Windows it writes every zero byte
            f.write(b"\0")
