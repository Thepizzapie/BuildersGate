"""A native WebView2 window, hosted through COM directly.

WHY THIS EXISTS

pywebview reaches WebView2 on Windows through .NET: pywebview -> winforms ->
pythonnet -> clr_loader -> hostfxr -> CLR -> WebView2. Every one of those links
has to be reassembled inside a PyInstaller bundle, and one of them does not
survive the trip:

    Failed to resolve Python.Runtime.Loader.Initialize
    from _internal/pythonnet/runtime/Python.Runtime.dll

Resolving an entrypoint out of Python.Runtime.dll needs a live .NET runtime
host, which hostfxr locates via a .runtimeconfig.json beside the assembly.
There is not one — not in the bundle and not in the source install either. From
source it works anyway because runtime discovery finds a machine-installed .NET;
inside a frozen app that discovery breaks down. The packaged app therefore had
no window at all, on a machine where the WebView2 runtime was installed and
working.

Patching the .NET hosting would be treating a symptom. WebView2 is a COM
component; .NET was only ever pywebview's chosen bridge to it. This module
talks to that COM API directly, so the entire fragile layer is gone rather than
repaired — in source builds and frozen builds alike.

WHAT IT NEEDS
  · WebView2Loader.dll — vendored by pywebview at
    webview/lib/runtimes/win-<arch>/native/, and locatable in a frozen bundle
    by the same relative path.
  · The Evergreen WebView2 Runtime, which ships with Windows 11.
  · comtypes, for implementing the COM completion handlers in Python.

WHAT IT DELIBERATELY DOES NOT DO
  No JS bridge, no native dialogs, no menus. This hosts a URL in a window that
  can be moved, resized and closed. The product is a web app served over
  loopback; the window is a frame around it.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path
from typing import Optional

# ── Win32 ────────────────────────────────────────────────────────────────────
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
CW_USEDEFAULT = -2147483648
SW_SHOW = 5
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_GETMINMAXINFO = 0x0024
IDC_ARROW = 32512

# ctypes.wintypes does not size these correctly for 64-bit, and getting them
# wrong shows up as "OverflowError: int too long to convert" the first time a
# real window message carries a pointer-sized lParam. Pin them explicitly.
LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WPARAM = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
LPARAM = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]

# ANYTHING RETURNING OR TAKING A HANDLE NEEDS A SIGNATURE.
# ctypes defaults an undeclared function to `int` in both directions, which is
# 32 bits — so a 64-bit handle is silently chopped. GetModuleHandleW(None)
# returns the image base, 0x7ff71d540000 on this machine; undeclared it came
# back as 0x1d540000. That is not a module, so the frozen build's
# LoadImage(hinst, MAKEINTRESOURCE(1)) found no icon resource and Windows
# substituted its generic application icon — the app shipped without its own
# icon in the taskbar and on the desktop, and nothing errored.
#
# Declaring restype alone is not enough: without argtypes, ctypes marshals the
# now-correct Python int back down to a C int at the call. Both ends, always.
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT),
                ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT),
                ("ptMaxTrackSize", POINT)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def loader_dll() -> Optional[Path]:
    """Find WebView2Loader.dll.

    pywebview vendors it, and PyInstaller keeps the package layout, so the same
    relative path works frozen and unfrozen. sys._MEIPASS is checked first
    because a onedir bundle puts data under _internal/ rather than beside the
    importable module.
    """
    arch = {"AMD64": "win-x64", "ARM64": "win-arm64",
            "x86": "win32"}.get((ctypes.sizeof(ctypes.c_void_p) == 8
                                 and "AMD64" or "x86"), "win-x64")
    rel = Path("webview") / "lib" / "runtimes" / arch / "native" / "WebView2Loader.dll"

    roots = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        roots.append(Path(base))
    try:
        import webview
        roots.append(Path(webview.__file__).parent.parent)
    except Exception:                                          # noqa: BLE001
        pass
    roots.append(Path(__file__).resolve().parent.parent)

    for root in roots:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    # Last resort: anything on the DLL search path already.
    return None


def available() -> tuple[bool, str]:
    """Can a window actually be opened here? Returns (ok, reason-if-not).

    Checked before the server starts, so a failure can be reported as a
    decision rather than as a crash halfway through startup.
    """
    if sys.platform != "win32":
        return False, "the native window is Windows-only"
    try:
        import comtypes  # noqa: F401
    except ImportError:
        return False, "comtypes is not installed"
    if loader_dll() is None:
        return False, "WebView2Loader.dll not found"
    return True, ""


# ── COM ──────────────────────────────────────────────────────────────────────
# WebView2 has no type library, so the vtables are declared by hand. Only the
# slots this module actually calls are defined; the rest are padded so the
# indices line up. Getting an index wrong is a hard crash rather than an
# exception, which is why each vtable below is written out in full order.
from ctypes import HRESULT, POINTER, c_void_p, c_wchar_p, c_int, byref, Structure  # noqa: E402
from ctypes import WINFUNCTYPE  # noqa: E402

S_OK = 0
E_NOINTERFACE = 0x80004002


class GUID(Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str = ""):
        super().__init__()
        if text:
            ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(text), byref(self))


IID_UNKNOWN = GUID("{00000000-0000-0000-C000-000000000046}")
IID_ENV_HANDLER = GUID("{4E8A3389-C9D8-4BD2-B6B5-124FEE6CC14D}")
IID_CTRL_HANDLER = GUID("{6C4819F3-C9B7-4260-8127-C9F5BDE7F68C}")

LPUNKNOWN = c_void_p


def _vtable(*methods):
    """Build a COM vtable struct from (name, prototype) pairs."""
    class VTable(Structure):
        _fields_ = [(name, proto) for name, proto in methods]
    return VTable


# IUnknown, shared by every interface below.
_QueryInterface = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
_AddRef = WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_Release = WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_Invoke2 = WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_void_p)


class _CompletionHandler:
    """A COM object, implemented in Python, for one async completion callback.

    WebView2's creation API is entirely asynchronous: you hand it an object
    implementing ICoreWebView2Create*CompletedHandler and it calls Invoke when
    the environment or controller is ready. comtypes can do this, but a hand-
    rolled vtable keeps the dependency surface to ctypes alone and makes the
    lifetime explicit — the handler must outlive the call, so it is held on the
    owning Window rather than left to the garbage collector.
    """

    def __init__(self, iid: GUID, on_complete):
        self._iid = iid
        self._on_complete = on_complete
        self._refs = 1

        def query(this, riid, out):
            wanted = riid.contents
            for known in (IID_UNKNOWN, self._iid):
                if bytes(wanted) == bytes(known):
                    out[0] = this
                    self._refs += 1
                    return S_OK
            out[0] = None
            return E_NOINTERFACE

        def add_ref(this):
            self._refs += 1
            return self._refs

        def release(this):
            self._refs -= 1
            return max(self._refs, 0)

        def invoke(this, hr, result):
            try:
                self._on_complete(hr, result)
            except Exception as exc:                           # noqa: BLE001
                print(f"webview2 handler failed: {exc}", file=sys.stderr)
            return S_OK

        # Keep strong references: if these are collected, COM calls into freed
        # memory and the process dies with no traceback.
        self._fns = (_QueryInterface(query), _AddRef(add_ref),
                     _Release(release), _Invoke2(invoke))
        VT = _vtable(("QueryInterface", _QueryInterface), ("AddRef", _AddRef),
                     ("Release", _Release), ("Invoke", _Invoke2))
        self._vt = VT(*self._fns)
        self._vt_ptr = ctypes.pointer(self._vt)
        self._obj = ctypes.pointer(ctypes.cast(self._vt_ptr, c_void_p))

    @property
    def ptr(self):
        return ctypes.cast(self._obj, c_void_p)


def _call(iface: c_void_p, index: int, restype, *argtypes):
    """Bind vtable slot `index` on a raw COM pointer."""
    vtbl = ctypes.cast(iface, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.cast(vtbl[index], WINFUNCTYPE(restype, c_void_p, *argtypes))
    return lambda *args: fn(iface, *args)


# Vtable slot indices, from the WebView2 SDK headers. IUnknown occupies 0-2 on
# every interface, so these all start at 3.
# ICoreWebView2Controller, in declaration order after IUnknown's 0-2:
#   3 get_IsVisible   4 put_IsVisible   5 get_Bounds   6 put_Bounds
#   7 get_ZoomFactor  8 put_ZoomFactor  ...  24 Close   25 get_CoreWebView2
#
# put_Bounds was 4 here, which is put_IsVisible — so every resize handed a RECT
# to a method expecting a BOOL, the bounds were never set, and the webview sat
# at 0x0 inside a correctly-created window. The symptom is a blank white pane
# with every HRESULT returning S_OK, because nothing actually failed.
_ENV_CREATE_CONTROLLER = 3      # ICoreWebView2Environment::CreateCoreWebView2Controller
_CTRL_PUT_BOUNDS = 6            # ICoreWebView2Controller::put_Bounds
_CTRL_GET_COREWEBVIEW2 = 25     # ICoreWebView2Controller::get_CoreWebView2
_WV_NAVIGATE = 5                # ICoreWebView2::Navigate


class Window:
    """One WebView2 window hosting a URL. Blocks in run() until closed."""

    def __init__(self, title: str, url: str, width: int = 1480, height: int = 940,
                 min_width: int = 820, min_height: int = 720,
                 user_data_dir: Optional[str] = None):
        self.title, self.url = title, url
        self.width, self.height = width, height
        self.min_width, self.min_height = min_width, min_height
        self.user_data_dir = user_data_dir
        self.hwnd = None
        self._controller = None
        self._webview = None
        self._handlers = []          # must outlive the async calls
        self._error: Optional[str] = None

    # ---- win32 ------------------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_SIZE and self._controller:
            self._resize()
            return 0
        if msg == WM_GETMINMAXINFO:
            mmi = ctypes.cast(lparam, POINTER(MINMAXINFO)).contents
            mmi.ptMinTrackSize.x = self.min_width
            mmi.ptMinTrackSize.y = self.min_height
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _resize(self):
        rect = RECT()
        user32.GetClientRect(self.hwnd, byref(rect))
        put_bounds = _call(self._controller, _CTRL_PUT_BOUNDS, HRESULT, RECT)
        put_bounds(rect)

    def _load_icon(self, hinst, big=True):
        """The app icon, for the title bar and the taskbar.

        Frozen, PyInstaller embeds packaging/icon.ico as resource id 1, so it
        comes straight out of the running executable. Failing that — and from
        source, where there is no resource to read — the .ico is loaded off
        disk.

        The resource path is tried first and the file path is a real fallback,
        not decoration: the resource lookup silently returned NULL for the whole
        of the first release because the module handle was being truncated to 32
        bits (see the signature block at the top of this file), and the only
        symptom was a generic Windows icon on the taskbar. A second, independent
        way to get the same bytes is worth the six lines.
        """
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        cx = user32.GetSystemMetrics(11 if big else 49)   # SM_CXICON / SM_CXSMICON
        cy = user32.GetSystemMetrics(12 if big else 50)

        if getattr(sys, "frozen", False):
            # MAKEINTRESOURCE(1): the resource id travels as the pointer value.
            icon = user32.LoadImageW(hinst, wt.LPCWSTR(1), IMAGE_ICON, cx, cy, 0)
            if icon:
                return icon
        for ico in self._icon_files():
            if ico.is_file():
                icon = user32.LoadImageW(None, str(ico), IMAGE_ICON, cx, cy,
                                         LR_LOADFROMFILE)
                if icon:
                    return icon
        return None

    @staticmethod
    def _icon_files():
        """Where icon.ico might be, source tree first then bundle root.

        sys._MEIPASS only exists frozen; bgate.spec puts the .ico at the bundle
        root so this second entry resolves there.
        """
        yield Path(__file__).resolve().parent.parent / "packaging" / "icon.ico"
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            yield Path(meipass) / "icon.ico"

    def _create_window(self):
        self._proc = WNDPROC(self._wndproc)      # strong ref or COM calls freed memory
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._proc
        cls.hInstance = kernel32.GetModuleHandleW(None)
        big = self._load_icon(cls.hInstance, big=True)
        small = self._load_icon(cls.hInstance, big=False)
        cls.hIcon = big or 0
        cls.hIconSm = small or 0
        cls.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        cls.hbrBackground = 5            # COLOR_WINDOW; repainted by the webview
        cls.lpszClassName = "BuildersGateWebView2"
        if not user32.RegisterClassExW(byref(cls)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):                 # 1410 = already registered
                raise OSError(f"RegisterClassEx failed ({err})")
        user32.CreateWindowExW.restype = wt.HWND
        self.hwnd = user32.CreateWindowExW(
            0, "BuildersGateWebView2", self.title,
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            CW_USEDEFAULT, CW_USEDEFAULT, self.width, self.height,
            None, None, cls.hInstance, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowEx failed ({ctypes.get_last_error()})")
        # The class icon covers most of it, but the class is registered once per
        # process and the shell reads the WINDOW icon when building the taskbar
        # button. Set both so neither depends on the other.
        WM_SETICON, ICON_BIG, ICON_SMALL = 0x0080, 1, 0
        if big:
            user32.SendMessageW(self.hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            user32.SendMessageW(self.hwnd, WM_SETICON, ICON_SMALL, small)
        user32.ShowWindow(self.hwnd, SW_SHOW)

    # ---- the async creation chain -----------------------------------------
    def _on_environment(self, hr, env):
        if hr != S_OK or not env:
            self._error = f"environment creation failed (hr=0x{hr & 0xFFFFFFFF:08X})"
            user32.PostQuitMessage(1)
            return
        handler = _CompletionHandler(IID_CTRL_HANDLER, self._on_controller)
        self._handlers.append(handler)
        create = _call(c_void_p(env), _ENV_CREATE_CONTROLLER,
                       HRESULT, wt.HWND, c_void_p)
        hr2 = create(self.hwnd, handler.ptr)
        if hr2 != S_OK:
            self._error = f"CreateCoreWebView2Controller failed (hr=0x{hr2 & 0xFFFFFFFF:08X})"
            user32.PostQuitMessage(1)

    def _on_controller(self, hr, controller):
        if hr != S_OK or not controller:
            self._error = f"controller creation failed (hr=0x{hr & 0xFFFFFFFF:08X})"
            user32.PostQuitMessage(1)
            return
        self._controller = c_void_p(controller)
        _call(self._controller, 1, ctypes.c_ulong)()          # AddRef: we keep it

        wv = c_void_p()
        get_wv = _call(self._controller, _CTRL_GET_COREWEBVIEW2,
                       HRESULT, POINTER(c_void_p))
        if get_wv(byref(wv)) != S_OK or not wv:
            self._error = "get_CoreWebView2 failed"
            user32.PostQuitMessage(1)
            return
        self._webview = wv
        self._resize()
        navigate = _call(wv, _WV_NAVIGATE, HRESULT, c_wchar_p)
        navigate(self.url)

    # ---- public -----------------------------------------------------------
    def run(self) -> Optional[str]:
        """Open the window and pump messages. Returns None, or an error string."""
        ok, why = available()
        if not ok:
            return why

        ctypes.oledll.ole32.CoInitializeEx(None, 2)   # STA; WebView2 requires it
        try:
            self._create_window()
            dll = ctypes.WinDLL(str(loader_dll()))
            handler = _CompletionHandler(IID_ENV_HANDLER, self._on_environment)
            self._handlers.append(handler)

            create_env = dll.CreateCoreWebView2EnvironmentWithOptions
            create_env.restype = HRESULT
            create_env.argtypes = [c_wchar_p, c_wchar_p, c_void_p, c_void_p]
            hr = create_env(None, self.user_data_dir, None, handler.ptr)
            if hr != S_OK:
                return f"CreateCoreWebView2EnvironmentWithOptions failed (hr=0x{hr & 0xFFFFFFFF:08X})"

            msg = wt.MSG()
            while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            return self._error
        finally:
            try:
                ctypes.oledll.ole32.CoUninitialize()
            except Exception:                                  # noqa: BLE001
                pass
