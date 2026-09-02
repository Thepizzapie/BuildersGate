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
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
CW_USEDEFAULT = -2147483648
SW_SHOW = 5
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
IDC_ARROW = 32512

# Our own message, handled in _wndproc. The page asks for a window drag over
# HTTP, and the drag MUST be started on the thread that owns the window — see
# start_drag().
WM_APP_STARTDRAG = 0x8001

# ── frameless window ────────────────────────────────────────────────────────
# Hit-test results. The window has no caption of its own, so the wndproc has to
# answer every one of these itself; DefWindowProc cannot, because as far as it
# is concerned the whole window is client area.
HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTBOTTOM = 10, 11, 12, 15
HTTOPLEFT, HTTOPRIGHT, HTBOTTOMLEFT, HTBOTTOMRIGHT = 13, 14, 16, 17

SM_CYCAPTION = 4
SM_CXSIZEFRAME, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 32, 33, 92

# How tall the page's own header is, in unscaled pixels, and how much of its
# right-hand end is buttons. The wndproc needs both to know which part of the
# top strip is a drag handle — it cannot ask the page, and a round trip per
# mouse-move would be absurd anyway. THESE MUST MATCH THE CSS in the shell's
# title bar (frontend/src/shell/TitleBar.tsx); they are asserted equal by
# tests/ui/test_webview2.py so the two cannot drift into a window you can only
# drag by half its bar.
CAPTION_H = 44
CAPTION_BUTTONS_W = 138          # three 46px buttons

def MAKEINTRESOURCE(i: int):
    """A numeric resource id where the API's signature says LPCWSTR.

    Win32 overloads these parameters: a string is a name, and a small integer
    stuffed into the pointer is an ordinal. Python cannot express that — with
    argtypes declaring LPCWSTR, handing ctypes an int raises

        argument 2: TypeError: 'int' object cannot be interpreted as ctypes.c_wchar_p

    and `wt.LPCWSTR(1)` raises the same thing, because c_wchar_p refuses to be
    built from an int. Going through c_void_p is the only way to put the ordinal
    in the pointer slot.

    THIS WAS BROKEN FOR THE LIFE OF THE FILE and nobody saw it, because
    available() returns False without comtypes, comtypes was declared nowhere,
    and so _create_window never ran outside a machine that happened to have it.
    Declaring the dependency is what made this reachable — and immediately
    fatal, on the first line that loads the window class cursor.
    """
    return ctypes.cast(ctypes.c_void_p(i), wt.LPCWSTR)


user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsZoomed.argtypes = [wt.HWND]
user32.GetDpiForWindow.restype = wt.UINT
user32.GetDpiForWindow.argtypes = [wt.HWND]
user32.ReleaseCapture.restype = wt.BOOL
user32.SetWindowPos.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]

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


class NCCALCSIZE_PARAMS(ctypes.Structure):
    """What WM_NCCALCSIZE points lParam at when wParam is TRUE.

    rgrc[0] is the proposed new CLIENT rectangle and is the only field written
    here: leaving it as the whole window rect is what removes the frame.
    rgrc[1] and rgrc[2] are the old client and window rects, which Windows uses
    to decide what to blit while resizing.
    """
    _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]


# ctypes.POINTER, not the bare name: the `from ctypes import POINTER` further
# down this file has not run yet at module-definition time.
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]


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
    # src/bgate_ui/window/ -> the repository root is three up.
    roots.append(Path(__file__).resolve().parents[3])

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
from ctypes import HRESULT, POINTER, c_void_p, c_wchar_p, byref, Structure  # noqa: E402
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
                 user_data_dir: Optional[str] = None, frameless: bool = False):
        self.title, self.url = title, url
        self.width, self.height = width, height
        self.min_width, self.min_height = min_width, min_height
        self.user_data_dir = user_data_dir
        self.frameless = bool(frameless)
        self.hwnd = None
        self._controller = None
        self._webview = None
        self._handlers = []          # must outlive the async calls
        self._error: Optional[str] = None

    # ---- frameless geometry ------------------------------------------------
    def _scale(self, px: int) -> int:
        """A CSS pixel in this window's device pixels.

        The caption height is shared with the page's CSS, which is laid out in
        CSS pixels; the hit test happens in device pixels. On a 150% display
        those differ by half again, and getting it wrong means the draggable
        strip and the drawn header do not line up — the top of the bar drags and
        the bottom selects text, or worse.
        """
        try:
            dpi = user32.GetDpiForWindow(self.hwnd) or 96
        except Exception:                                       # noqa: BLE001
            dpi = 96
        return int(round(px * dpi / 96.0))

    def _border(self) -> int:
        """Width of the invisible resize border, in device pixels."""
        return (user32.GetSystemMetrics(SM_CXSIZEFRAME)
                + user32.GetSystemMetrics(SM_CXPADDEDBORDER)) or 8

    def _caption_inset(self) -> int:
        """How much DefWindowProc took off the top for the caption it draws.

        Given back in WM_NCCALCSIZE so the page can draw its own bar there.
        Measured rather than assumed: it is the system caption height plus the
        top resize border, and both move with the display's scaling.
        """
        return (user32.GetSystemMetrics(SM_CYCAPTION)
                + user32.GetSystemMetrics(SM_CYSIZEFRAME)
                + user32.GetSystemMetrics(SM_CXPADDEDBORDER))

    def _hittest(self, lparam) -> int:
        """Which part of a frameless window the cursor is over.

        With no caption there is nothing for DefWindowProc to find, so every
        answer — resize edges, corners, and the strip that drags the window —
        is computed here. Get this wrong and the window cannot be resized or
        moved at all, which is the failure mode people report as "it froze".
        """
        rect = RECT()
        user32.GetWindowRect(self.hwnd, byref(rect))
        # lParam is two SIGNED shorts. Masking without sign-extending puts the
        # cursor at x=65500 the moment it crosses the left edge of the screen.
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value

        b = self._border()
        maximized = bool(user32.IsZoomed(self.hwnd))
        if not maximized:
            left = x < rect.left + b
            right = x >= rect.right - b
            top = y < rect.top + b
            bottom = y >= rect.bottom - b
            if top and left:     return HTTOPLEFT
            if top and right:    return HTTOPRIGHT
            if bottom and left:  return HTBOTTOMLEFT
            if bottom and right: return HTBOTTOMRIGHT
            if top:              return HTTOP
            if bottom:           return HTBOTTOM
            if left:             return HTLEFT
            if right:            return HTRIGHT

        # The drag strip: the page's header, minus the window buttons at its
        # right-hand end. Returning HTCAPTION here is what gives back the whole
        # native gesture set for free — drag to move, double-click to maximize,
        # drag to a screen edge to snap, shake to minimise everything else.
        if y < rect.top + self._scale(CAPTION_H):
            if x < rect.right - self._scale(CAPTION_BUTTONS_W):
                return HTCAPTION
        return HTCLIENT

    # ---- win32 ------------------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCCALCSIZE and self.frameless and wparam:
            # Returning 0 with the rect left alone makes the CLIENT AREA the
            # whole window: no caption, no visible border, but still a real
            # top-level window that snaps, animates and casts a shadow. That is
            # the whole trick, and it is why this is not a WS_POPUP — a popup
            # loses all of those.
            #
            # LETTING DefWindowProc COMPUTE THE RECT AND GIVING BACK ONLY THE
            # CAPTION DOES NOT WORK, and it is a tempting mistake because it
            # sounds tidier. WM_NCCALCSIZE decides where the client area IS; it
            # does not decide what the non-client area PAINTS. Grow the client
            # up over the caption and Windows draws its caption anyway, on top —
            # the window ends up wearing two title bars, the system's and ours.
            # Measured, on screen, exactly once.
            #
            # The resize edges this appears to cost are handled in _resize(),
            # which stops the webview short of them.
            #
            # MAXIMIZED IS THE SPECIAL CASE and it is not optional. Windows
            # sizes a maximized window slightly LARGER than the work area, on
            # the assumption that the frame it is about to draw will absorb the
            # difference. With no frame, nothing absorbs it, and the top of the
            # page — the title bar, the only way to unmaximize — sits offscreen.
            if user32.IsZoomed(hwnd):
                params = ctypes.cast(lparam, POINTER(NCCALCSIZE_PARAMS)).contents
                inset = self._border()
                params.rgrc[0].left += inset
                params.rgrc[0].top += inset
                params.rgrc[0].right -= inset
                params.rgrc[0].bottom -= inset
            return 0
        if msg == WM_NCHITTEST and self.frameless:
            return self._hittest(lparam)
        if msg == WM_APP_STARTDRAG and self.frameless:
            # Runs on the WINDOW'S OWN THREAD, which is the point — see
            # start_drag(). ReleaseCapture from any other thread is a no-op, and
            # WM_NCLBUTTONDOWN sent from one enters a modal move loop on the
            # wrong thread.
            #
            # lParam CARRIES THE GRAB POINT and it is not optional: DefWindowProc
            # anchors the move to it, so passing 0 anchors the drag to the top
            # left of the primary monitor and the window does not follow the
            # mouse at all. Read now rather than passed in, because the press
            # travelled here as an HTTP request and the cursor has moved since.
            pt = POINT()
            user32.GetCursorPos(byref(pt))
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION,
                                (pt.y << 16) | (pt.x & 0xFFFF))
            return 0
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

    # ---- window controls, driven by the page -------------------------------
    # The page reaches these over the loopback API it is already talking to
    # (see bgate_ui/routes/window.py), NOT over a WebView2 message channel.
    # Adding one would mean hand-writing another COM handler and pinning
    # another vtable index by hand, for a button that has a perfectly good HTTP
    # route available in the same process.
    #
    # All three post rather than call: they run on the server's thread, and
    # every one of these must happen on the thread that owns the window.
    def minimize(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, 0x0112, 0xF020, 0)   # WM_SYSCOMMAND SC_MINIMIZE

    def toggle_maximize(self) -> None:
        if self.hwnd:
            cmd = 0xF120 if user32.IsZoomed(self.hwnd) else 0xF030   # SC_RESTORE / SC_MAXIMIZE
            user32.PostMessageW(self.hwnd, 0x0112, cmd, 0)

    def close(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def start_drag(self) -> None:
        """Begin a window move, as if the user had grabbed a real caption.

        THE PAGE CANNOT DRAG THE WINDOW BY ITSELF and neither can the parent's
        hit test: WebView2 puts a child window over the whole client area, and a
        child window consumes the mouse before WM_NCHITTEST is ever asked. So
        the title bar reports its own mousedown over HTTP and the window hands
        the gesture to Windows, which then runs its ordinary modal move loop —
        edge snapping, multi-monitor and all — against a mouse button that is
        already physically down.

        Posted rather than done here: ReleaseCapture only affects the calling
        thread, and this is called from the server's. The wndproc does the work
        on the window's own thread.
        """
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_STARTDRAG, 0, 0)

    def is_maximized(self) -> bool:
        return bool(self.hwnd and user32.IsZoomed(self.hwnd))

    def _resize(self):
        rect = RECT()
        user32.GetClientRect(self.hwnd, byref(rect))
        if self.frameless and not user32.IsZoomed(self.hwnd):
            # STOP THE PAGE SHORT OF THE RESIZE EDGES.
            #
            # Frameless means the client area is the whole window, so a webview
            # filling the client area covers the pixels the resize borders live
            # in — and a CHILD WINDOW TAKES THE MOUSE BEFORE THE PARENT IS
            # ASKED, so _hittest never sees them and the window cannot be
            # resized at all. Reported as "I can't move the window", which was
            # the same cause: the drag strip is covered too.
            #
            # Holding back a border's width on three sides gives those pixels
            # to the parent, where WM_NCHITTEST answers HTLEFT / HTRIGHT /
            # HTBOTTOM and the corners. The window background is painted the
            # app's own ground (see _create_window), so the reserved edge reads
            # as a thin border rather than as a white seam.
            #
            # The TOP is deliberately not inset: the title bar has to reach the
            # top of the window or there is a gap above it. Top-edge resizing is
            # the one gesture given up, and it is the least used of the eight.
            # Dragging is restored separately, over HTTP — see start_drag().
            b = self._border()
            rect.left += b
            rect.right -= b
            rect.bottom -= b
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
            icon = user32.LoadImageW(hinst, MAKEINTRESOURCE(1), IMAGE_ICON, cx, cy, 0)
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
        yield Path(__file__).resolve().parents[3] / "packaging" / "icon.ico"
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            yield Path(meipass) / "icon.ico"

    @staticmethod
    def _claim_taskbar_identity():
        """Tell the shell this process is Builders Gate, not whatever launched it.

        THE TASKBAR DOES NOT USE THE WINDOW ICON unless the process has an
        explicit AppUserModelID. Without one Windows groups the button under the
        host executable's identity and draws ITS icon — so running from source
        the taskbar showed python.exe's icon no matter what hIcon and WM_SETICON
        were set to, and the window's own icon was never wrong.

        Frozen it matters less, because the host executable IS BuildersGate.exe
        and carries the right icon anyway. It is still set in both cases so the
        button groups under one identity rather than two, and so a pinned
        shortcut keeps working across a source/frozen switch.

        Failure is ignored: this is cosmetic, and shell32 is not worth crashing
        a window over.
        """
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wt.LPCWSTR]
            shell32.SetCurrentProcessExplicitAppUserModelID("Thepizzapie.BuildersGate")
        except Exception:                                       # noqa: BLE001
            pass

    def _create_window(self):
        self._claim_taskbar_identity()
        self._proc = WNDPROC(self._wndproc)      # strong ref or COM calls freed memory
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._proc
        cls.hInstance = kernel32.GetModuleHandleW(None)
        big = self._load_icon(cls.hInstance, big=True)
        small = self._load_icon(cls.hInstance, big=False)
        cls.hIcon = big or 0
        cls.hIconSm = small or 0
        cls.hCursor = user32.LoadCursorW(None, MAKEINTRESOURCE(IDC_ARROW))
        # THE APP'S OWN GROUND, not COLOR_WINDOW. Frameless holds back a
        # border's width around the webview for the resize edges (see _resize),
        # and COLOR_WINDOW painted that reserve white — a bright seam around a
        # near-black app. gdi32 wants BGR, so #0a0a0c is 0x0c0a0a.
        cls.hbrBackground = (gdi32.CreateSolidBrush(0x0C0A0A)
                             if self.frameless else 5)
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
        if self.frameless:
            # THE CALL WITHOUT WHICH THE WHOLE FRAMELESS PATH IS DEAD CODE.
            #
            # CreateWindowEx computes the frame ONCE, and it does so without
            # ever sending WM_NCCALCSIZE with wParam TRUE — the only form our
            # handler acts on. So the handler never ran, the client rect kept
            # its 31px of caption, and the window came up wearing BOTH the
            # system caption and the page's own title bar. Every other part of
            # the frameless work was correct and none of it was reachable.
            #
            # SWP_FRAMECHANGED forces the recalculation, which delivers the
            # wParam TRUE message. Measured with a standalone probe: top inset
            # 31px before this call, 0px after.
            SWP_FRAMECHANGED, SWP_NOMOVE, SWP_NOSIZE = 0x0020, 0x0002, 0x0001
            SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
            user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE
                                | SWP_NOZORDER | SWP_NOACTIVATE)
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
    def set_title(self, text: str) -> bool:
        """Retitle the window. Safe to call from another thread.

        SetWindowTextW posts WM_SETTEXT rather than running code on the caller's
        thread, so the badge watcher can use it without touching the message
        pump. Returns False before the window exists (the watcher starts first
        by design — a badge is not worth ordering the startup around).
        """
        if not self.hwnd:
            return False
        try:
            self.title = str(text)
            return bool(user32.SetWindowTextW(self.hwnd, self.title))
        except Exception:                                      # noqa: BLE001
            return False

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
