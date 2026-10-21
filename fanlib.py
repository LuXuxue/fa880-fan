#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fanlib -- shared plumbing for fanctl.py, fanspeed.py and fanguard.py.

=============================================================================
The fan control interface
=============================================================================
Fan control on this machine goes through the ACPI PNP0C14 WMI interface:

    namespace : root\\WMI
    class     : PowerSwitchInterface
    instance  : ACPI\\PNP0C14\\IP3POWERSWITCH_0
    methods   : SetFanControl(FanNumber, FanDuty) -> ResultStatus
                GetFanControl(FanNumber)          -> FanDuty

FanDuty encoding:

    101       AUTO    firmware / EC thermal curve takes back over
    100       MAX     100 % duty
    35..100   MANUAL  fixed duty, percent

FanNumber:
    1 = CPU fan
    2 = memory (GPU) fan

GetFanControl packs BOTH current RPMs into one uint32, regardless of which
FanNumber you ask for:

    bits 15..0  -> fan 1 (CPU)    RPM
    bits 31..16 -> fan 2 (memory) RPM

fan 1 is the LOW half.  Confirmed on hardware: driving FanNumber 1 to 100%
makes the low half climb past 4700 RPM while the high half stays put, and
vice versa.

Measured MAX plateaus on an FEVM FA880 PRO (Ryzen 7 8845HS):
    fan 1 (CPU)    ~4700 RPM
    fan 2 (memory) ~2400 RPM

The interface takes a single FanNumber per call, so setting the two fans
independently is just two separate calls.

Reading root\\WMI PowerSwitchInterface requires Administrator.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import ctypes
import os
import sys
import tempfile
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

WMI_CLASS = "PowerSwitchInterface"
DEFAULT_INSTANCE = r"ACPI\PNP0C14\IP3POWERSWITCH_0"

DUTY_AUTO = 101           # out-of-range sentinel == "let firmware drive"
DUTY_MAX = 100
GUI_MIN = 35              # lowest duty we accept

FAN_CPU = 1
FAN_MEM = 2
FAN_LABEL = {FAN_CPU: "CPU fan", FAN_MEM: "Memory fan"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ACCESS = 2

# Per-fan gauge full scale, i.e. each fan's MAX duty plateau.
#
# Measured on an FEVM FA880 PRO (Ryzen 7 8845HS) by driving each fan to
# 100 % duty and reading back the steady-state speed:
#     fan 1 (CPU)    tops out around 4700 RPM
#     fan 2 (memory) tops out around 2410 RPM
#
# A single shared scale makes the slower fan look permanently half-empty, so
# each bar is scaled to its own ceiling instead.  Adjust these two numbers if
# your machine's fans spin faster or slower than these.
FAN_FULL_SCALE = {
    FAN_CPU: 4800,
    FAN_MEM: 2500,
}


class FrameWriter:
    """Redraw a fixed block of text in place, without growing scrollback.

    Clearing the whole screen (ESC[2J) makes the terminal scroll once the
    frame is taller than the window, and pushes a copy of every refresh into
    the scrollback.  Instead we remember how many lines the previous frame
    used, move the cursor back up that far, erase from there down and rewrite
    -- so the display neither scrolls nor accumulates history.
    """

    def __init__(self, use_ansi: bool):
        self.use_ansi = use_ansi
        self._prev_lines = 0

    def write(self, text: str) -> None:
        text = text.rstrip("\n")
        if not self.use_ansi:
            # Not a real terminal (piped to a file, etc): just append.
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            return

        if self._prev_lines:
            # Later frames: jump back to where the last frame started and
            # erase from there down.  The banner above stays put.
            sys.stdout.write("\x1b[%dA" % self._prev_lines)
            sys.stdout.write("\x1b[J")
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        self._prev_lines = text.count("\n") + 1

    def banner(self, text: str) -> None:
        """Print a one-off line above the live area."""
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


class NullWriter:
    """Discards everything -- for the service, which has no console."""

    def write(self, text: str) -> None:
        pass

    def banner(self, text: str) -> None:
        pass


# --------------------------------------------------------------------------
# console setup
# --------------------------------------------------------------------------

def init_console() -> None:
    """Make UTF-8 output work on legacy Windows consoles (cp936 etc.).

    Under pythonw (or any GUI-hosted python) sys.stdout/sys.stderr are None;
    give them a null sink so code that prints does not explode.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def title(text: str) -> None:
    """Print a heading, in colour only on a real terminal."""
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\033[1;36m%s\033[0m\n" % text)
            return
    except Exception:
        pass
    print(text)


class Tee:
    """Duplicate everything written to a stream onto a log file.

    The elevated child runs in a hidden console, so the parent has to relay
    its output back to the terminal the user is actually looking at.
    """

    def __init__(self, original, log):
        self._original = original
        self._log = log

    def write(self, text):
        for stream in (self._original, self._log):
            try:
                stream.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self):
        for stream in (self._original, self._log):
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        # The child's console is not the user's console; skip ANSI colour.
        return False

    @property
    def encoding(self):
        return "utf-8"


def install_elev_log(path: str) -> None:
    """Called in an elevated child: mirror all output into `path`."""
    try:
        log = open(path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = Tee(sys.stdout, log)
    sys.stderr = Tee(sys.stderr, log)


# --------------------------------------------------------------------------
# elevation
# --------------------------------------------------------------------------

class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_INFINITE = 0xFFFFFFFF
_WAIT_TIMEOUT = 0x00000102


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _spawn_elevated(extra_args=None, quiet=False):
    """ShellExecuteEx('runas') this script again.

    The child's output is relayed back through a file, because the elevated
    child gets a hidden console of its own.  With quiet=True the caller has
    no console to show that output in (pythonw), so the file is skipped
    entirely and the child writes to the null device instead -- a relay log
    that nobody reads is pure disk traffic.

    Returns (info, log_path) on success, or (None, None) if UAC was declined.
    """
    if quiet:
        log_path = os.devnull                 # "nul" -- nothing hits the disk
    else:
        handle, log_path = tempfile.mkstemp(prefix="fanctl-", suffix=".log")
        os.close(handle)

    # lpFile is the interpreter, so lpParameters must hold the SCRIPT and its
    # arguments only -- including sys.executable here would make the child run
    # `python.exe "python.exe" "fanspeed.py" ...`, which fails instantly with
    # its error going to the hidden console.
    argv = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    argv += ["--no-elevate", "--elev-log", log_path,
             "--parent-pid", str(os.getpid())]
    if extra_args:
        argv += list(extra_args)
    params = " ".join('"{}"'.format(a) for a in argv)

    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = params
    info.nShow = 0                      # hide the child's console window
    info.lpDirectory = os.path.dirname(os.path.abspath(sys.argv[0]))

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        print("UAC elevation was declined or failed (error %d)."
              % ctypes.get_last_error(), file=sys.stderr)
        if not quiet:
            try:
                os.unlink(log_path)
            except OSError:
                pass
        return None, None
    return info, log_path


def _exit_code(handle) -> int:
    code = wintypes.DWORD()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetExitCodeProcess(handle, ctypes.byref(code))
    return int(code.value)


def _wait_for_child(handle) -> int:
    """Wait for the elevated child to exit and return its exit code."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.WaitForSingleObject(handle, _INFINITE)
    return _exit_code(handle)


def elevate_once() -> int:
    """Run the requested command elevated and print its output.  One-shot."""
    info, log_path = _spawn_elevated()
    if info is None:
        return EXIT_ERROR

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        k32.WaitForSingleObject(info.hProcess, _INFINITE)
        code = _exit_code(info.hProcess)

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            with open(log_path, "rb") as fh:
                sys.stdout.write(decoder.decode(fh.read()))
                sys.stdout.flush()
        except OSError:
            pass
        return code
    except Exception as exc:
        print("failed to elevate: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    finally:
        try:
            k32.CloseHandle(info.hProcess)
            os.unlink(log_path)
        except OSError:
            pass


def relay_child_output(handle, log_path) -> int:
    """Print the child's log to our stdout until it exits.  Returns its code.

    Split out from elevate_stream so the tail loop can be exercised without
    going through UAC.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    rc = EXIT_OK
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                if k32.WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
                    break
                chunk = fh.read()
                if chunk:
                    sys.stdout.write(decoder.decode(chunk))
                    sys.stdout.flush()
                else:
                    time.sleep(0.1)
                if close_requested():
                    # The window is closing: stop the child so it can hand the
                    # fans back to AUTO instead of being killed with them
                    # still pinned.
                    raise KeyboardInterrupt
            # anything written just before the process exited
            chunk = fh.read()
            if chunk:
                sys.stdout.write(decoder.decode(chunk))
                sys.stdout.flush()

        code = wintypes.DWORD()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        rc = int(code.value)
    except KeyboardInterrupt:
        rc = 130
        # Do not wait for the child here.  It watches us through --parent-pid
        # and shuts down within a fraction of a second of our exit (fanguard
        # restores AUTO on its way out), so waiting for it would freeze the
        # window for the full timeout on every Ctrl+C.
        pass
    return rc


def elevate_stream(quiet: bool = False) -> int:
    """Run elevated and stream its output live until the child exits.

    With quiet=True the child's output is thrown away instead of being
    relayed: the caller has no console to show it in, so routing it through
    a file in %TEMP% would only cost disk writes.  The child is still
    waited for -- it watches our death and would stop the fans otherwise.
    """
    extra = ["--ansi"] if _stdout_is_tty() else []
    info, log_path = _spawn_elevated(extra, quiet=quiet)
    if info is None:
        return EXIT_ERROR

    if quiet:
        rc = _wait_for_child(info.hProcess)
    else:
        rc = relay_child_output(info.hProcess, log_path)

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        k32.WaitForSingleObject(info.hProcess, 5000)
        k32.CloseHandle(info.hProcess)
        if not quiet:
            os.unlink(log_path)
    except OSError:
        pass
    return rc


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


_SYNCHRONIZE = 0x00100000


def parent_handle(parent_pid=None):
    """Open a SYNCHRONIZE handle to the parent process.

    The elevated child runs in its own hidden console, so closing the user's
    window sends it no CTRL_CLOSE_EVENT and it would otherwise survive as an
    orphan.  Holding a handle to the parent lets it notice the parent's death.
    Returns None when there is no parent to watch (or it is already gone).
    """
    if not parent_pid:
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    try:
        handle = k32.OpenProcess(_SYNCHRONIZE, False, int(parent_pid))
    except Exception:
        return None
    return handle or None


def parent_gone(handle) -> bool:
    """True once the watched parent process has exited."""
    if not handle:
        return False          # nothing to watch: run forever
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return k32.WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT


def ensure_admin(reason=None):
    """If we are not elevated, re-launch elevated and return the new exit code.

    Returns None when the caller may proceed.
    """
    if is_admin():
        return None
    if reason:
        print(reason)
    else:
        print("This tool needs Administrator to touch root\\WMI %s." % WMI_CLASS)
    print("Requesting elevation...")
    return elevate_once()


# --------------------------------------------------------------------------
# WMI layer
# --------------------------------------------------------------------------

class FanError(Exception):
    pass


class AccessDenied(FanError):
    pass


class FanInterface:
    """Thin wrapper around the PowerSwitchInterface WMI methods."""

    def __init__(self):
        try:
            import win32com.client
        except ImportError:
            raise FanError("pywin32 is required:  pip install pywin32")

        self._win32com = win32com.client
        self.instance = self._discover_instance()
        # A machine without PowerSwitchInterface (or a broken WMI service)
        # raises com_error here, which is not a FanError -- callers only
        # catch FanError, so translate it rather than leaking a traceback.
        try:
            self._svc = win32com.client.GetObject("winmgmts:/root/WMI")
        except Exception as exc:
            raise FanError("cannot reach root\\WMI: %s" % exc)
        self.last_packed = 0

    def _discover_instance(self) -> str:
        """Find the PowerSwitchInterface instance, falling back to the
        well-known name."""
        try:
            coll = self._win32com.GetObject(
                r"winmgmts:\\.\root\WMI:" + WMI_CLASS)
            for item in coll:
                name = item.Name
                if name and WMI_CLASS in name:
                    inner = name.split("=", 1)[1].strip().strip("'")
                    if inner:
                        return inner
        except Exception:
            pass
        return DEFAULT_INSTANCE

    @property
    def _objpath(self) -> str:
        return "%s.InstanceName='%s'" % (WMI_CLASS, self.instance)

    def _exec(self, method: str, **params):
        try:
            obj = self._svc.Get(self._objpath)
            in_params = obj.Methods_(method).inParameters.SpawnInstance_()
            for key, value in params.items():
                setattr(in_params, key, value)
            return self._svc.ExecMethod(self._objpath, method, in_params)
        except (AccessDenied, FanError):
            raise
        except Exception as exc:
            # Reading the instance itself is denied without Administrator, so
            # the AccessDenied check must wrap the whole call.
            if is_access_denied(exc):
                raise AccessDenied(
                    "access denied talking to root\\WMI %s -- Administrator "
                    "is required" % WMI_CLASS)
            raise FanError("%s failed: %s" % (method, exc))

    def get_rpm(self):
        """Return (fan1_rpm, fan2_rpm) for FanNumber 1 and 2.

        fan1 occupies the LOW 16 bits, fan2 the HIGH 16 bits.
        """
        out = self._exec("GetFanControl", FanNumber=FAN_CPU)
        packed = int(out.FanDuty)
        self.last_packed = packed
        return packed & 0xFFFF, (packed >> 16) & 0xFFFF

    def set_duty(self, fan: int, duty: int) -> int:
        """Set one fan.  Does NOT touch the other fan."""
        out = self._exec("SetFanControl", FanNumber=fan, FanDuty=int(duty))
        return int(out.ResultStatus)

    def dump(self):
        """Print the raw WMI method signatures."""
        title("WMI method signatures  (root\\WMI\\%s)" % WMI_CLASS)
        try:
            coll = self._win32com.GetObject(
                r"winmgmts:\\.\root\WMI:" + WMI_CLASS)
            names = [c.Name for c in coll]
        except Exception:
            names = [self.instance]
        print("  instances    : %s" % ", ".join(names))
        print("  selected     : %s" % self.instance)
        for method in ("SetFanControl", "GetFanControl"):
            try:
                obj = self._svc.Get(self._objpath)
                meta = obj.Methods_(method)
                ins = [p.Name for p in meta.inParameters.Properties_]
                outs = [p.Name for p in meta.outParameters.Properties_]
                print("  %-14s in(%s) -> out(%s)"
                      % (method, ", ".join(ins), ", ".join(outs)))
            except Exception as exc:
                print("  %-14s <unavailable: %s>" % (method, exc))
        return EXIT_OK


def is_access_denied(exc: Exception) -> bool:
    text = str(exc)
    return ("0x80041021" in text or "WBEM_E_ACCESS_DENIED" in text
            or "拒绝访问" in text or "Access is denied" in text)


# --------------------------------------------------------------------------
# mode handling
# --------------------------------------------------------------------------

AUTO_WORDS = {"auto", "automatic", "default", "on", "自动"}
MAX_WORDS = {"max", "maximum", "full", "100", "最大"}


def parse_mode(text: str) -> int:
    """'auto'/'自动' -> 101, 'max'/'最大' -> 100, '35'..'100' -> that value."""
    key = str(text).strip().lower()
    if key in AUTO_WORDS:
        return DUTY_AUTO
    if key in MAX_WORDS:
        return DUTY_MAX
    if key.isdigit():
        value = int(key)
        if not (GUI_MIN <= value <= 100):
            raise argparse.ArgumentTypeError(
                "duty must be %d..100, or 'auto', or 'max' (got %d)"
                % (GUI_MIN, value))
        return value
    raise argparse.ArgumentTypeError(
        "unknown mode %r -- use 'auto', 'max', or a number %d..100"
        % (text, GUI_MIN))


def describe(duty: int) -> str:
    if duty == DUTY_AUTO:
        return "AUTO (firmware curve)"
    if duty == DUTY_MAX:
        return "MAX (100%)"
    return "MANUAL %d%%" % duty


def setting_label(duty) -> str:
    """Short form for display: AUTO / MAX / NN% / ?"""
    if duty is None:
        return "?"
    if duty == DUTY_AUTO:
        return "AUTO"
    if duty == DUTY_MAX:
        return "MAX"
    return "%d%%" % duty


def parse_gear(text) -> int:
    """'AUTO' -> 101, 'MAX' -> 100, '70%' -> 70, anything else -> None.

    The governor publishes the gear it applied as a label, so this is the
    way back from a telemetry sample to the duty value.
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text or text == "?":
        return None
    if text.upper() == "AUTO":
        return DUTY_AUTO
    if text.upper() == "MAX":
        return DUTY_MAX
    try:
        return int(text.rstrip("%"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

BAR_WIDTH = 28


def rpm_bar(rpm, full_scale, width=BAR_WIDTH) -> str:
    """A gauge where full_scale means 100 % -- pass each fan's own ceiling."""
    if not rpm or rpm <= 0:
        return "[" + "." * width + "]"
    if full_scale <= 0:
        full_scale = 1
    filled = min(width, int(round(rpm * width / float(full_scale))))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def format_rpm(rpm) -> str:
    """rpm is an int, or None when the reading could not be taken."""
    if rpm is None:
        return "?"
    return "%d RPM" % rpm if rpm else "stopped"


def fan_rows(fan1, fan2, cpu=None, mem=None) -> list:
    """The two fan lines: RPM gauge plus the gear, when it is known.

    cpu/mem are duty values (or None when the gear cannot be told); the RPMs
    are ints, or None when the reading could not be taken.
    """
    return [
        "  %-12s %s  %-9s  of %-5s %s" % (
            FAN_LABEL[FAN_CPU], rpm_bar(fan1, FAN_FULL_SCALE[FAN_CPU]),
            format_rpm(fan1), FAN_FULL_SCALE[FAN_CPU],
            setting_label(cpu)),
        "  %-12s %s  %-9s  of %-5s %s" % (
            FAN_LABEL[FAN_MEM], rpm_bar(fan2, FAN_FULL_SCALE[FAN_MEM]),
            format_rpm(fan2), FAN_FULL_SCALE[FAN_MEM],
            setting_label(mem)),
    ]


def gear_note(cpu=None, mem=None, source=None) -> str:
    """One line saying where the displayed gear came from."""
    if cpu is None and mem is None:
        return ("  setting: unknown -- the firmware reports RPM only, and no "
                "governor is publishing the gear it applied")
    where = " (%s)" % source if source else ""
    return "  setting: CPU %s, memory %s%s" % (setting_label(cpu),
                                                setting_label(mem), where)


def report(fan: FanInterface) -> None:
    """One-shot snapshot of both fans."""
    fan1, fan2 = fan.get_rpm()
    cpu, mem, source = live_gears()
    title("Fan status")
    print("  instance      : %s" % fan.instance)
    for row in fan_rows(fan1, fan2, cpu, mem):
        print(row)
    print(gear_note(cpu, mem, source))
    print("  raw FanDuty   : 0x%08X   (fan1 = low 16 bits, fan2 = high 16)"
          % fan.last_packed)
    print("  gauges are scaled to each fan's own MAX duty plateau.")


# --------------------------------------------------------------------------
# single-instance guard
# --------------------------------------------------------------------------
# Two governors running at once would fight over the fans: each writes
# SetFanControl, both publish to the same shared memory, and either one
# exiting hands the fans back to AUTO underneath the other.  A named mutex
# keeps the second one out.  The kernel drops it when the process dies, so
# a force-kill cannot leave a stale lock behind.

# The governor may run as a Windows service, which lives in session 0, while
# the tools run in the user's session.  Objects in the "Local\" namespace are
# per session, so the two would never meet: the service has to use the
# global namespace instead.
#
# Creating a global object needs the SeCreateGlobalPrivilege, which only
# services have -- a user process cannot create one, but it may open one.
# So every name is tried in turn and the first one that works is used.
GLOBAL_TELEMETRY_NAME = "Global\\fa880-fan-telemetry"
GLOBAL_MUTEX_NAME = "Global\\fa880-fan-fanguard"
LOCAL_TELEMETRY_NAME = "Local\\fa880-fan-telemetry"
LOCAL_MUTEX_NAME = "Local\\fa880-fan-fanguard"

TELEMETRY_NAMES = (GLOBAL_TELEMETRY_NAME, LOCAL_TELEMETRY_NAME)
MUTEX_NAMES = (GLOBAL_MUTEX_NAME, LOCAL_MUTEX_NAME)

_WAIT_OBJECT_0 = 0


def acquire_single_instance(names=MUTEX_NAMES):
    """Claim the right to be the only running governor.

    -> (handles, message).  handles is None when another instance already
    holds the lock.  Keep the handles for as long as you want the lock.

    Every name is tried: the global one first (that is what a service in
    session 0 uses), then the session-local one.  A user process cannot
    *create* a global object, but it may open one -- so when the creation
    fails for lack of the privilege, the name is probed instead, and an
    existing lock is still found.

    Note that a mutex which merely *exists* is not a lock: the object stays
    behind as long as any handle to it is open, so ownership is tested by
    trying to take it.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES),
                                 wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                wintypes.LPCWSTR]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    handles = []
    with security_attributes(mutex_sddl()) as attributes:
        for index, name in enumerate(names):
            ctypes.set_last_error(0)
            handle = k32.CreateMutexW(
                ctypes.byref(attributes) if attributes else None, False, name)
            if handle:
                if k32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0:
                    # Somebody else holds it.
                    k32.CloseHandle(handle)
                    release_single_instance(handles)
                    return None, ("another fanguard instance is already "
                                  "running")
                handles.append(handle)
                continue
            error = ctypes.get_last_error()
            # Cannot create it -- but it may exist already, and then a
            # governor is running.  A user process cannot create global
            # objects at all.
            #
            # Only the existence can be checked here: a handle opened with
            # just SYNCHRONIZE access cannot test the ownership (a mutex
            # needs MUTEX_MODIFY_STATE for that).  In practice the two are
            # the same thing, because a governor holds its handle for its
            # whole life.
            probe = k32.OpenMutexW(_SYNCHRONIZE, False, name)
            if probe:
                k32.CloseHandle(probe)
                release_single_instance(handles)
                return None, ("another fanguard instance is already "
                              "running")
            if index + 1 < len(names):
                continue          # try the next name
            release_single_instance(handles)
            return None, ("could not create the single-instance mutex "
                          "(error %d)" % error)
    return handles, "single-instance lock acquired"


def release_single_instance(handles) -> None:
    if not handles:
        return
    if not isinstance(handles, (list, tuple)):
        handles = [handles]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    for handle in handles:
        try:
            k32.ReleaseMutex(handle)
        except Exception:
            pass
        try:
            k32.CloseHandle(handle)
        except Exception:
            pass


def governor_present(names=MUTEX_NAMES) -> bool:
    """True if the single-instance lock is alive, i.e. a governor is running.

    This is the authoritative "a governor is running" signal.  The telemetry
    bus only proves that somebody published recently, so a governor that is
    alive but wedged (hung sensor driver, stuck WMI call) would look dead
    there while it still holds the lock and still owns the fans.

    The lock object only goes away when the last handle to it is closed, and
    a governor keeps its handle for its whole life -- so the object being
    there says the same thing as somebody owning it.

    Probed with OpenMutexW(SYNCHRONIZE) rather than CreateMutexW:
      * OpenMutexW never creates the object, so a probe cannot make a
        governor that is starting up look like a duplicate;
      * it asks for no write access, so a lower-integrity caller is not
        blocked by the integrity label of an elevated governor's mutex.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    for name in names:
        handle = k32.OpenMutexW(_SYNCHRONIZE, False, name)
        if handle:
            k32.CloseHandle(handle)
            return True
    return False


# --------------------------------------------------------------------------
# console close handling
# --------------------------------------------------------------------------
# Closing the console window (the X button) raises CTRL_CLOSE_EVENT.  Python's
# default handler terminates the process outright, which would skip the
# "hand the fans back to AUTO" cleanup and leave the fans at the last
# commanded duty.  A console control handler lets us notice the request and
# shut down in an orderly way instead.
#
# The handler runs on its own thread, so it must return quickly -- it only
# sets a flag, and the main loop does the actual cleanup.

_CLOSE_REQUESTED = False
_CONSOLE_HANDLER = None

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2


def _console_handler(event):
    global _CLOSE_REQUESTED
    if event in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
        # Let the chain continue so CPython raises KeyboardInterrupt as usual
        # (and we still exit with 130).  Swallowing it here would make a
        # Ctrl+C look like a successful run.
        return False
    # CTRL_CLOSE_EVENT and friends: Python has no handler for them, so we
    # take it and shut down in an orderly way instead of being killed.
    _CLOSE_REQUESTED = True
    return True


def install_console_handler() -> bool:
    """Ask Windows to tell us about a window close instead of killing us."""
    global _CONSOLE_HANDLER
    if _CONSOLE_HANDLER is not None:
        return True
    try:
        # BOOL is a 32-bit int in the Windows ABI; c_bool is one byte and
        # would leave the upper bits of the return register undefined.
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        _CONSOLE_HANDLER = proto(_console_handler)
        k32 = ctypes.WinDLL("kernel32")
        k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        if not k32.SetConsoleCtrlHandler(_CONSOLE_HANDLER, True):
            _CONSOLE_HANDLER = None
            return False
        return True
    except Exception:
        _CONSOLE_HANDLER = None
        return False


def close_requested() -> bool:
    """True once the user closed the window or pressed Ctrl+C."""
    return _CLOSE_REQUESTED


# --------------------------------------------------------------------------
# kernel object permissions
# --------------------------------------------------------------------------
# The governor may run as a Windows service, i.e. as LocalSystem, while
# fanspeed runs as the logged-on user.  A named object created by SYSTEM
# inherits SYSTEM's default DACL, and a medium-integrity user cannot open
# that -- so the telemetry bus and the single-instance mutex are created
# with an explicit DACL instead.

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


# The access rights are written out in hex, because SDDL's short aliases
# are a trap: "SD" means DELETE, and SYNCHRONIZE has no alias at all -- the
# word is rejected by the SDDL parser (error 1336).
SYNCHRONIZE_MASK = "0x00100000"

TELEMETRY_SDDL = "D:(A;;GA;;;WD)"
MUTEX_SDDL = ("D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;%s;;;WD)" % SYNCHRONIZE_MASK)


@contextlib.contextmanager
def security_attributes(sddl):
    """SECURITY_ATTRIBUTES for a named kernel object, or None on failure.

    The descriptor is built from an SDDL string:

        "D:(A;;GA;;;WD)"
            everyone may read and write  (the telemetry bus: it carries
            nothing but temperatures and fan speeds)

        "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100000;;;WD)"
            SYSTEM and the administrators group have full control, everyone
            else may only open it for synchronisation -- enough to probe
            whether a governor is running, not enough to steal its lock

    The descriptor is freed when the block ends: it only has to stay alive
    while the object is being created.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = \
        ctypes.c_void_p
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        yield None
        return

    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    attributes.lpSecurityDescriptor = descriptor
    attributes.bInheritHandle = False
    try:
        yield attributes
    finally:
        kernel32.LocalFree(descriptor)


def _current_user_sid():
    """The SID of the account this process runs as, as an SDDL string."""
    try:
        import win32api
        import win32security
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(
            token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(sid)
    except Exception:
        return None


def mutex_sddl():
    """The SDDL for the single-instance mutex.

    SYSTEM and the administrators group get full control, everybody else
    may only open it for synchronisation -- enough to ask whether a
    governor is running, not enough to steal its lock.

    The creator needs full control as well: an explicit DACL grants nothing
    by itself, so the account that creates the mutex is put into it.
    """
    sid = _current_user_sid()
    if sid:
        return ("D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)(A;;%s;;;WD)"
                % (sid, SYNCHRONIZE_MASK))
    return MUTEX_SDDL


# --------------------------------------------------------------------------
# Windows service
# --------------------------------------------------------------------------
# The governor may run as a Windows service, so the tools that report or
# change the fan state want to know what that service is doing.
#
# Reading a service's state needs no elevation: every user may query the
# status of a service.

SERVICE_NAME = "fa880-fan"

SERVICE_STATE_NAMES = {
    1: "STOPPED",
    2: "START_PENDING",
    3: "STOP_PENDING",
    4: "RUNNING",
    5: "CONTINUE_PENDING",
    6: "PAUSE_PENDING",
    7: "PAUSED",
}


def service_state(name=SERVICE_NAME):
    """-> the state of a Windows service ("RUNNING", "STOPPED", ...).

    None means the service is not installed (or cannot be queried).
    Reading a service's state needs no elevation.
    """
    try:
        import win32service
        hscm = win32service.OpenSCManager(None, None,
                                          win32service.SC_MANAGER_CONNECT)
        try:
            handle = win32service.OpenService(
                hscm, name, win32service.SERVICE_QUERY_STATUS)
        except Exception:
            return None
        try:
            status = win32service.QueryServiceStatus(handle)
        finally:
            win32service.CloseServiceHandle(handle)
        return SERVICE_STATE_NAMES.get(status[1], "?")
    except Exception:
        return None


def service_running(name=SERVICE_NAME) -> bool:
    """True when the governor service is up and running."""
    return service_state(name) == "RUNNING"


# --------------------------------------------------------------------------
# live telemetry bus
# --------------------------------------------------------------------------
# The governor publishes its latest sample here; fanspeed (which would
# otherwise need Administrator just to read RPMs) picks it up without ever
# touching WMI or the disk.
#
# A named file mapping is used because it carries no security prompt and
# needs no file on disk.  The writer is usually the service, which runs as
# LocalSystem in session 0, so the mapping lives in the global namespace
# (see TELEMETRY_NAMES) and carries an explicit DACL that lets the
# logged-on user read it.
#
# Layout:  [uint32 seq][uint32 length][payload ...]
# The writer bumps `seq` last, so a reader that sees the same seq before and
# after the copy knows it did not tear.

TELEMETRY_CAPACITY = 1024
TELEMETRY_MAX_AGE = 20.0          # seconds before a sample counts as stale

FILE_MAP_READ = 0x0002
FILE_MAP_ALL_ACCESS = 0x000F001F

# The names themselves (and why there is a global and a session-local one)
# are defined with the single-instance guard further up.


class TelemetryBus:
    """Named shared memory holding fanguard's most recent sample."""

    def __init__(self, capacity=TELEMETRY_CAPACITY, names=None):
        self.capacity = capacity
        # The global name first: that is the one a service uses, and the
        # one the tools have to find.
        self.names = tuple(names) if names else TELEMETRY_NAMES
        self._handle = None
        self._ptr = None
        self._seq = 0
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileMappingW.restype = wintypes.HANDLE
        self._k32.CreateFileMappingW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
        self._k32.OpenFileMappingW.restype = wintypes.HANDLE
        self._k32.OpenFileMappingW.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self._k32.MapViewOfFile.restype = wintypes.LPVOID
        self._k32.MapViewOfFile.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_size_t]
        # Without this, ctypes narrows a 64-bit address to c_int and raises
        # "OverflowError: int too long to convert" on the way out.
        self._k32.UnmapViewOfFile.argtypes = [wintypes.LPVOID]
        self._k32.UnmapViewOfFile.restype = wintypes.BOOL
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]

    # -- mapping ------------------------------------------------------------

    def _map(self, writable):
        # Map again only after releasing the old view: otherwise the handle
        # and the view of the previous mapping leak.
        self.close()
        access = FILE_MAP_ALL_ACCESS if writable else FILE_MAP_READ

        with security_attributes(TELEMETRY_SDDL) as attributes:
            for name in self.names:
                # Prefer opening an existing mapping: creating a global one
                # needs a privilege only services have, and a user process
                # must not put its own session-local mapping in the way.
                handle = self._k32.OpenFileMappingW(access, False, name)
                if not handle and writable:
                    # Only the writer creates the mapping.  A reader that
                    # made a session-local one of its own would keep reading
                    # that one, even after the service comes up with the
                    # global one.
                    handle = self._k32.CreateFileMappingW(
                        -1, ctypes.byref(attributes) if attributes else None,
                        0x00000004, 0, self.capacity, name)
                if not handle:
                    continue
                # FILE_MAP_WRITE (0x0004) maps READ-ONLY on this build of
                # Windows even though the section itself is PAGE_READWRITE --
                # writing through it faults.  FILE_MAP_ALL_ACCESS works;
                # readers still use FILE_MAP_READ.
                ptr = self._k32.MapViewOfFile(handle, access, 0, 0, 0)
                if not ptr:
                    self._k32.CloseHandle(handle)
                    continue
                self._handle, self._ptr = handle, ptr
                return True
        return False

    def open_writer(self):
        return self._map(writable=True)

    def open_reader(self):
        return self._map(writable=False)

    def close(self):
        if self._ptr is not None:
            self._k32.UnmapViewOfFile(self._ptr)
            self._ptr = None
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    # -- primitives ---------------------------------------------------------

    def _get_u32(self, offset):
        return ctypes.cast(self._ptr + offset,
                           ctypes.POINTER(ctypes.c_uint32))[0]

    def _set_u32(self, offset, value):
        ctypes.cast(self._ptr + offset,
                    ctypes.POINTER(ctypes.c_uint32))[0] = value

    # -- api ----------------------------------------------------------------

    def publish(self, text):
        """Store one sample.  Returns False if the bus is unavailable."""
        if self._ptr is None and not self.open_writer():
            return False
        raw = text.encode("utf-8", "replace")[:self.capacity - 8]
        ctypes.memmove(self._ptr + 8, raw, len(raw))
        self._set_u32(4, len(raw))
        self._seq = (self._seq + 1) or 1
        self._set_u32(0, self._seq)          # bump last: publishes are atomic
        return True

    def read(self):
        """Return the sample as a dict, or None.  Never raises."""
        if self._ptr is None and not self.open_reader():
            return None
        for _ in range(3):
            seq = self._get_u32(0)
            if seq == 0:
                return None
            length = self._get_u32(4)
            if length == 0 or length > self.capacity - 8:
                return None
            raw = ctypes.string_at(self._ptr + 8, length)
            if self._get_u32(0) == seq:       # unchanged => not torn
                return parse_sample(raw.decode("utf-8", "replace"))
        return None

    @staticmethod
    def is_live(sample, max_age=TELEMETRY_MAX_AGE):
        if not sample:
            return False
        wall = sample.get("wall")
        try:
            return (time.time() - float(wall)) <= max_age
        except (TypeError, ValueError):
            return False


def parse_sample(text):
    """'wall=... pid=... cpu=...' -> dict."""
    out = {}
    for token in text.split():
        if "=" in token:
            key, value = token.split("=", 1)
            out[key] = value
    return out or None


def build_sample(wall, pid, temps, rpms, duties):
    """Assemble the one-line payload fanguard publishes."""
    rpm_cpu, rpm_mem = rpms if rpms else (None, None)
    return (
        "wall=%.3f pid=%d cpu=%s mem=%s cpu_rpm=%s mem_rpm=%s "
        "duty_cpu=%s duty_mem=%s"
        % (wall, pid,
           _fmt_num(temps.get("cpu")), _fmt_num(temps.get("mem")),
           rpm_cpu if rpm_cpu is not None else "-",
           rpm_mem if rpm_mem is not None else "-",
           duties[0], duties[1]))


def gears_from_sample(sample):
    """(cpu_duty, mem_duty) out of a telemetry sample, or (None, None)."""
    if not sample:
        return None, None
    return (parse_gear(sample.get("duty_cpu")),
            parse_gear(sample.get("duty_mem")))


def live_gears(max_age=TELEMETRY_MAX_AGE):
    """The gear a running governor is applying, read from the bus.

    There is no way to read the duty back from the hardware: GetFanControl
    returns packed *RPM*, and PowerSwitchInterface exposes no getter for the
    setting.  So the only live source of the gear is the governor that set
    it, and it publishes it on the bus -- no file on disk involved.

    -> (cpu_duty, mem_duty, source).  The duties are None when no governor
    is publishing, i.e. when the gear cannot be told at all.
    """
    bus = TelemetryBus()
    try:
        if not bus.open_reader():
            return None, None, None
        sample = bus.read()
    finally:
        bus.close()
    if not TelemetryBus.is_live(sample, max_age):
        return None, None, None
    cpu, mem = gears_from_sample(sample)
    return cpu, mem, "fanguard"


def _fmt_num(value):
    return "%.1f" % value if value is not None else "-"
