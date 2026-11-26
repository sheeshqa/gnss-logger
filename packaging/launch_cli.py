from gnss_logger.cli import main

if __name__ == '__main__':
    import ctypes
    import sys
    handle = None
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, False, 'Local\\GNSSLogger.CLI')
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        raise SystemExit(main())
    finally:
        if handle:
            kernel.CloseHandle(handle)
