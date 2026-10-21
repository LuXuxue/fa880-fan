#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fanguard -- temperature-driven background fan controller.

Watches CPU and memory temperatures through LibreHardwareMonitor and steers the
two fans.  The firmware AUTO curve on this machine never reaches full speed, so
the CPU sits hot enough to cost performance; driving the fans from the actual
temperature fixes that.

Control law, per fan:

    temp >= --cpu-max-at / --mem-max-at   ->  MAX (100 %)
    temp in between                      ->  linear ramp between
                                            --min-duty and 100 %
    temp <  --auto-below                 ->  hand back to the firmware AUTO
                                            curve, which may stop the fan

Below the cold threshold the fan is released so it can spin down for quiet;
a small hysteresis band stops it flip-flopping at the boundary.

    fanguard                      run with defaults, live display
    fanguard --dry-run            log decisions, never touch the fans
    fanguard --cpu-max-at 68 --mem-max-at 72

As a Windows service (the normal way to run it):

    fanguard --service            started by the service control manager
    fanguard --install-service    register it (Administrator)
    fanguard --uninstall-service  remove it again (Administrator)
    fanguard --start-service      start the installed service
    fanguard --stop-service       stop it (the fans go back to AUTO)

Once installed it comes up with the machine and is controlled with the
usual tools:

    net start fa880-fan      net stop fa880-fan
    sc  start fa880-fan      sc  stop fa880-fan

Sensors used (auto-detected, first match wins):

    CPU     "Core (Tctl/Tdie)"          -> drives the CPU fan
    memory  max("DIMM #0", "DIMM #1")  -> drives the memory fan

The LibreHardwareMonitor assemblies live in ./vendor/LibreHardwareMonitor next
to this file, so the script is portable -- nothing is read from C:\\Portable or
any other fixed location.

Requires Administrator, for the WMI fan interface.  Reading the sensors
additionally needs LibreHardwareMonitor's own PawnIO driver, which it loads
by itself -- nothing has to be installed.  Needs pythonnet:
pip install pythonnet

Safety: on exit -- normally, on Ctrl+C, when the console window is closed,
or when the service is stopped -- the fans are handed back to the firmware
AUTO curve, unless --no-restore is given.  A hard kill (taskkill /F) cannot
be caught and does skip that.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time

import fanlib as fan

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(HERE, "vendor", "LibreHardwareMonitor")

DEFAULT_INTERVAL = 3.0
DEFAULT_STEP = 5
DEFAULT_MIN = fan.GUI_MIN
DEFAULT_MAX = 100

# Below this the fan is handed back to AUTO so the firmware can stop it.
AUTO_BELOW = 50
# At/above these the fan goes to full speed, which is what keeps the CPU from
# thermally throttling under load.
CPU_MAX_AT = 70
MEM_MAX_AT = 75
DEFAULT_HYST = 3

# Under pythonw (the background daemon) there is no console at all: sys.stdout
# is None and nobody can see the live display.  The elevated child must then
# write to the null device instead of a relay log in %TEMP% that would grow
# for as long as the daemon runs.  Captured here, before init_console()
# replaces the missing stream with a sink.
QUIET = sys.stdout is None


# --------------------------------------------------------------------------
# temperature source
# --------------------------------------------------------------------------

CPU_NAMES = ("Core (Tctl/Tdie)", "Core (Tctl)", "Core (Tdie)",
             "CPU Package", "CPU Tctl/Tdie")
MEM_PREFIX = "DIMM"


class Thermometer:
    """Reads CPU and memory temperatures out of LibreHardwareMonitorLib."""

    def __init__(self, vendor_dir=VENDOR_DIR):
        self.vendor_dir = vendor_dir
        self._computer = None
        self._cpu = None
        self._mem = []

    # -- setup -------------------------------------------------------------

    def start(self):
        dll = os.path.join(self.vendor_dir, "LibreHardwareMonitorLib.dll")
        if not os.path.isfile(dll):
            raise fan.FanError(
                "LibreHardwareMonitorLib.dll not found in %s\n"
                "Copy LibreHardwareMonitorLib.dll plus its dependencies\n"
                "(RAMSPDToolkit-NDD.dll, System.Memory.dll, ...) and\n"
                "LibreHardwareMonitor.config into that folder."
                % self.vendor_dir)

        # The .NET loader probes the process working directory for dependent
        # assemblies (System.Memory, RAMSPDToolkit-NDD, ...), so point it at
        # the vendor/ directory while we load, then put cwd back.
        previous = os.getcwd()
        os.environ["PATH"] = self.vendor_dir + os.pathsep + os.environ["PATH"]
        try:
            import clr
            os.chdir(self.vendor_dir)
            clr.AddReference(dll)
            from LibreHardwareMonitor.Hardware import Computer

            comp = Computer()
            comp.IsCpuEnabled = True
            comp.IsGpuEnabled = False
            comp.IsMemoryEnabled = True
            comp.IsStorageEnabled = False
            comp.IsNetworkEnabled = False
            comp.IsPsuEnabled = False
            comp.IsControllerEnabled = False
            comp.Open()
        except Exception as exc:
            raise fan.FanError("LibreHardwareMonitor failed to start: %s" % exc)
        finally:
            try:
                os.chdir(previous)
            except OSError:
                pass

        self._computer = comp
        self._bind()

    def _bind(self):
        """Pick the sensors we care about, tolerating naming differences."""
        self._cpu = None
        self._mem = []
        for hw in self._computer.Hardware:
            kind = str(hw.HardwareType)
            try:
                hw.Update()
            except Exception:
                pass
            sensors = list(hw.Sensors)
            if kind == "Cpu" and self._cpu is None:
                self._cpu = _pick(sensors, CPU_NAMES)
            elif kind == "Memory":
                for s in sensors:
                    if str(s.SensorType) == "Temperature" and \
                            str(s.Name).startswith(MEM_PREFIX):
                        self._mem.append(s)

    def stop(self):
        if self._computer is not None:
            try:
                self._computer.Close()
            except Exception:
                pass
            self._computer = None

    # -- reading -----------------------------------------------------------

    def read(self):
        """-> dict with 'cpu' and 'mem' keys (None when unavailable)."""
        for hw in self._computer.Hardware:
            try:
                hw.Update()
            except Exception:
                pass

        temps = [v for v in (_value(s) for s in self._mem) if v is not None]
        return {
            "cpu": _value(self._cpu),
            "mem": max(temps) if temps else None,
        }

    def describe(self):
        cpu = self._cpu.Name if self._cpu is not None else "?"
        mem = ", ".join(str(s.Name) for s in self._mem) or "?"
        return "CPU: %s | memory: %s" % (cpu, mem)

    def has(self, name):
        """True if this sensor was found and can be expected to report.

        A machine may simply not have the sensor at all (no SPD temperature
        on the DIMMs, an unusual CPU sensor name).  Those must not be
        mistaken for a sensor that died -- otherwise fanguard would give up
        on a machine that is perfectly readable.
        """
        if name == "cpu":
            return self._cpu is not None
        if name == "mem":
            return bool(self._mem)
        return False


def _pick(sensors, names):
    by_name = {str(s.Name): s for s in sensors}
    for name in names:
        if name in by_name:
            return by_name[name]
    return None


def _value(sensor):
    if sensor is None:
        return None
    try:
        v = sensor.Value
    except Exception:
        return None
    return None if v is None else float(v)


# --------------------------------------------------------------------------
# control law
# --------------------------------------------------------------------------

class Governor:
    """One fan: AUTO when cold, a linear ramp in between, MAX at the ceiling."""

    def __init__(self, name, auto_below, max_at, hyst,
                 minimum, maximum, step):
        self.name = name
        self.auto_below = auto_below
        self.max_at = max_at
        self.hyst = hyst
        self.minimum = minimum
        self.maximum = maximum
        self.step = max(1, step)
        # The duty the curve asks for.  decide() sets it on every call.
        self.duty = minimum
        # What is really in effect.  self.duty is the *target* and is set
        # before the write is attempted, so a rejected duty must not be
        # reported as if it had taken effect.  None means "unknown" -- we
        # never managed to set it, so whatever the firmware left behind is
        # still running.
        self.applied = None
        self.auto = True             # begin where the firmware would
        self.last_action = ""

    # -- helpers -----------------------------------------------------------

    def map_duty(self, temp):
        """Linear ramp from minimum at auto_below to maximum at max_at."""
        span = float(self.max_at - self.auto_below)
        if span <= 0:
            return self.maximum
        frac = (temp - self.auto_below) / span
        frac = max(0.0, min(1.0, frac))
        return self.minimum + (self.maximum - self.minimum) * frac

    def _quantise(self, value):
        """Floor to a step so MAX is only ever reached at max_at."""
        return _clamp(int(value // self.step) * self.step,
                      self.minimum, self.maximum)

    def current(self):
        return fan.DUTY_AUTO if self.auto else self.duty

    def duty_text(self, dry_run=False):
        """The gear to show.

        Live: the duty really in effect.  "?" when we do not know it,
        because a value we never successfully set is not the same as AUTO.

        Dry-run: nothing is ever applied, so the meaningful number is the
        decision itself -- that is the whole point of the mode.
        """
        if dry_run:
            return fan.setting_label(self.duty)
        if self.applied is None:
            return "?"
        return fan.setting_label(self.applied)

    def rule_text(self):
        return "AUTO <%d C, MAX >=%d C" % (self.auto_below, self.max_at)

    # -- the decision ------------------------------------------------------

    def decide(self, temp):
        """-> (duty to apply, action text).  duty may be DUTY_AUTO."""
        if temp is None:
            self.duty = self.current()
            return self.duty, "no reading"

        # MAX is an absolute guarantee: never sit in AUTO while the fan
        # should be flat out, whatever auto_below happens to be.
        if temp >= self.max_at:
            self.auto = False
            self.duty = self.maximum
            return self.duty, "%.1f C >= %d C, MAX" % (temp, self.max_at)

        if self.auto:
            # Only take over once it is clearly warm, otherwise we would
            # bounce between AUTO and manual every tick around the boundary.
            if temp < self.auto_below + self.hyst:
                self.duty = fan.DUTY_AUTO
                return self.duty, "%.1f C, still cold, AUTO" % temp
            self.auto = False
        elif temp < self.auto_below:
            self.auto = True
            self.duty = fan.DUTY_AUTO
            return self.duty, "%.1f C < %d C, AUTO" % (temp, self.auto_below)

        self.duty = self._quantise(self.map_duty(temp))
        return self.duty, "%.1f C -> %d%%" % (temp, self.duty)


def _clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

class Log:
    """Kept in memory by default.

    A file is only written when --log is given explicitly: the service runs
    unattended and should not grow a log file forever, and touching the disk
    on every tick is pointless wear.
    """

    HISTORY = 500

    def __init__(self, path=None):
        self.fh = None
        self.history = collections.deque(maxlen=self.HISTORY)
        if path:
            try:
                self.fh = open(path, "a", encoding="utf-8", buffering=1)
                self.fh.write("# %s fanguard started %s\n"
                              % (time.strftime("%Y-%m-%d"),
                                 time.strftime("%H:%M:%S")))
            except OSError as exc:
                print("cannot open log %s: %s" % (path, exc), file=sys.stderr)

    def row(self, stamp, temps, cpu, mem, rpms=None, dry_run=False):
        # rpms is optional -- the measured speeds, which are also published
        # to the bus so a non-elevated viewer can show them without WMI
        # access of its own.
        # In dry-run the duty column records the *decision*, since nothing is
        # ever applied and the applied value would always be meaningless.
        rpm_cpu, rpm_mem = rpms if rpms else (None, None)
        self.text("%s cpu=%s mem=%s cpu_rpm=%s mem_rpm=%s "
                  "duty_cpu=%s duty_mem=%s"
                  % (stamp, _fmt(temps["cpu"]), _fmt(temps["mem"]),
                     rpm_cpu if rpm_cpu is not None else "-",
                     rpm_mem if rpm_mem is not None else "-",
                     cpu.duty_text(dry_run).strip(),
                     mem.duty_text(dry_run).strip()))

    def text(self, message):
        self.history.append(message)
        if self.fh:
            self.fh.write(message + "\n")

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def _fmt(value):
    return "%.1f" % value if value is not None else "-"


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def render_frame(temps, cpu, mem, started, interval, failures,
                 dry_run=False):
    lines = [
        "",
        "  fanguard   %s   (+%ds)"
        % (time.strftime("%H:%M:%S"), int(time.time() - started)),
        "",
        "  temperature      fan          duty     rule",
        "  CPU  %6s C   %-12s %-7s  %s"
        % (_fmt(temps["cpu"]), cpu.name, cpu.duty_text(dry_run),
           cpu.rule_text()),
        "  MEM  %6s C   %-12s %-7s  %s"
        % (_fmt(temps["mem"]), mem.name, mem.duty_text(dry_run),
           mem.rule_text()),
        "",
        "  %s | %s" % (cpu.last_action, mem.last_action),
    ]
    if failures:
        lines.append("  WARNING: %d consecutive sensor read failures" % failures)
    lines.append("  every %gs -- Ctrl+C to stop (fans return to AUTO)" % interval)
    return "\n".join(lines)


def run(args, thermo, fan_iface, log, extra_stop=None, display=True,
        publish=False):
    cpu = Governor("CPU fan", args.auto_below, args.cpu_max_at,
                   args.hysteresis, args.min_duty,
                   args.max_duty, args.step)
    mem = Governor("Memory fan", args.auto_below, args.mem_max_at,
                   args.hysteresis, args.min_duty,
                   args.max_duty, args.step)

    if display:
        writer = fan.FrameWriter(getattr(args, "ansi", False)
                                 or sys.stdout.isatty())
    else:
        writer = fan.NullWriter()      # the service has no console
    parent = fan.parent_handle(getattr(args, "parent_pid", None))
    started = time.time()

    # Publish each tick into shared memory so a non-elevated fanspeed can
    # display live speeds without WMI access of its own.
    bus = fan.TelemetryBus()
    if not bus.open_writer():
        bus = None

    failures = 0
    failed = False
    last = {"cpu": None, "mem": None}
    decided = {"cpu": None, "mem": None}   # dry-run bookkeeping only
    next_tick = started
    deadline = started + args.duration if args.duration > 0 else None

    try:
        while not fan.parent_gone(parent):
            if extra_stop is not None and extra_stop():
                # The service control manager asked us to stop.
                writer.banner("stop requested by the service manager")
                break
            if fan.close_requested():
                # The window was closed: shut down in an orderly way so the
                # finally block can hand the fans back to AUTO.
                writer.banner("window closed, shutting down")
                break
            if deadline is not None and time.time() >= deadline:
                writer.banner("duration reached, stopping")
                break
            # Sensor failure detection must count *unavailable readings*, not
            # just exceptions: Thermometer.read() absorbs update errors and
            # reports a missing sensor as None, so a dead sensor would
            # otherwise never trip the counter and the fans would stay pinned
            # at the last commanded duty forever.
            #
            # Only sensors that were actually bound are counted: a machine
            # without SPD temperature on its DIMMs reports mem=None on every
            # tick, and that is not a failure -- it is just how the machine
            # is.  Such a fan is left to the firmware (AUTO).
            try:
                temps = thermo.read()
                error = None
            except Exception as exc:
                temps = {"cpu": None, "mem": None}
                error = str(exc)

            missing = [k for k in ("cpu", "mem")
                       if thermo.has(k) and temps[k] is None]
            if missing:
                failures += 1
                if error:
                    log.text("# sensor error: %s" % error)
            else:
                failures = 0

            if args.max_failures > 0 and failures >= args.max_failures:
                log.text("# %d consecutive ticks with no reading for %s, "
                         "bailing out" % (failures, "/".join(missing)))
                writer.banner("sensor failure, giving up")
                # Not a clean stop: the service has to exit with an error
                # code, or the service control manager would treat it as a
                # normal stop and never run the failure actions.
                failed = True
                break

            for gov, key, number in ((cpu, "cpu", fan.FAN_CPU),
                                     (mem, "mem", fan.FAN_MEM)):
                new, action = gov.decide(temps[key])
                gov.last_action = "%s: %s" % (gov.name, action)
                if new == last[key]:
                    continue
                if args.dry_run:
                    # Never commit anything in dry-run: the fans were not
                    # touched, so recording it would publish a gear that is
                    # not in effect.  Keep a separate "decided" value so the
                    # same decision is not logged on every tick.
                    if new != decided[key]:
                        decided[key] = new
                        log.text("# dry-run would set %s -> %s"
                                 % (gov.name,
                                    "AUTO" if new == fan.DUTY_AUTO
                                    else "%d%%" % new))
                    continue
                try:
                    status = fan_iface.set_duty(number, new)
                except fan.FanError as exc:
                    # Leave last[key] alone so the next tick retries: a
                    # transient COM/WMI error must not skip a step of the
                    # control curve for good.
                    log.text("# %s set failed: %s" % (gov.name, exc))
                    continue
                if status != 0:
                    log.text("# %s rejected duty=%d (ResultStatus=%d)"
                             % (gov.name, new, status))
                    continue
                last[key] = new
                gov.applied = new       # now it is really in effect

            rpms = None
            if not args.dry_run:
                try:
                    rpms = fan_iface.get_rpm()
                except fan.FanError:
                    rpms = None
            log.row(time.strftime("%Y-%m-%d %H:%M:%S"), temps, cpu, mem,
                    rpms, args.dry_run)
            if bus is not None and publish:
                bus.publish(fan.build_sample(
                    time.time(), os.getpid(), temps, rpms,
                    (cpu.duty_text().strip(), mem.duty_text().strip())))
            writer.write(render_frame(temps, cpu, mem, started,
                                      args.interval, failures,
                                      args.dry_run))

            next_tick += args.interval
            delay = next_tick - time.time()
            if delay < 0:
                next_tick = time.time()
                delay = 0
            # sleep in slices so Ctrl+C / a window close / the parent's death
            # is noticed promptly
            slept = 0.0
            while slept < delay:
                time.sleep(min(0.25, delay - slept))
                slept += 0.25
                if fan.close_requested() or fan.parent_gone(parent):
                    break          # the outer loop re-checks and exits
    finally:
        if parent:
            try:
                import ctypes
                ctypes.WinDLL("kernel32").CloseHandle(parent)
            except Exception:
                pass
        if bus is not None:
            bus.close()      # release the shared mapping + its handle

    return fan.EXIT_ERROR if failed else 0


# --------------------------------------------------------------------------
# Windows service
# --------------------------------------------------------------------------
# The governor runs as a Windows service: it comes up with the machine, it is
# controlled with the usual tools, and it survives the user logging out.
#
#     net start fa880-fan      net stop fa880-fan
#     sc  start fa880-fan      sc  stop fa880-fan
#
# It runs as LocalSystem -- the WMI fan interface and the PawnIO sensor
# driver both need administrator rights -- and publishes its samples on the
# shared memory bus, so fanspeed still works without asking for elevation.

SERVICE_NAME = fan.SERVICE_NAME
SERVICE_DISPLAY_NAME = "FA880 Fan Governor"
SERVICE_DESCRIPTION = ("CPU / memory fan governor for the FA880 PRO: both "
                       "fans are driven from the measured temperature.")

EVENT_SOURCE = "fa880-fan"

# Event ids.  They are arbitrary but must stay stable.
EVT_STARTED = 1
EVT_STOPPED = 2
EVT_ERROR = 3
EVT_INFO = 4

SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFO = 4


class EventLog:
    """The service's only visible output.

    A service has no console, so without the event log a refusal to start
    or a crash would be completely silent.  Only life cycle events and
    errors are reported -- never one line per tick.
    """

    def __init__(self, source=EVENT_SOURCE):
        self.source = source
        self._registered = False

    def register(self) -> bool:
        """Make sure the event source exists (needs Administrator).

        The installer registers it, so in the normal case there is nothing
        to write -- and the registry hive is a file on disk.
        """
        if self._registered:
            return True
        try:
            import win32api
            import win32con
            key = (r"SYSTEM\CurrentControlSet\Services\EventLog\Application"
                   r"\%s" % self.source)
            try:
                handle = win32api.RegOpenKeyEx(
                    win32con.HKEY_LOCAL_MACHINE, key, 0, win32con.KEY_READ)
            except Exception:
                pass
            else:
                win32api.RegCloseKey(handle)
                self._registered = True
                return True
            import win32evtlogutil
            win32evtlogutil.AddSourceToRegistry(self.source)
            self._registered = True
        except Exception:
            self._registered = False
        return self._registered

    def report(self, event_id, message, severity=SEVERITY_INFO) -> None:
        try:
            import win32evtlogutil
            if not self._registered:
                self.register()
            win32evtlogutil.ReportEvent(self.source, event_id, 0, severity,
                                        (str(message),))
        except Exception:
            pass          # the event log must never break the governor


class Reporter:
    """Where the governor's messages go.

    Foreground: the console.  Service: the Windows event log, because a
    service has no console to print to.
    """

    def __init__(self, echo=True, event_log=None):
        self.echo = echo
        self.event_log = event_log

    def say(self, message, severity=SEVERITY_INFO) -> None:
        if self.echo:
            print(message)
        if self.event_log is not None:
            self.event_log.report(EVT_INFO, message, severity)


def _build_service_class():
    """Create the ServiceFramework subclass (pywin32 must be importable)."""
    import win32serviceutil

    class _Service(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_requested = False
            self.event_log = EventLog()

        # -- SCM callbacks ---------------------------------------------------

        def SvcDoRun(self):
            self.event_log.report(EVT_STARTED, "fanguard service starting")
            try:
                code = govern(SERVICE_ARGS, drives_fans=True,
                              reporter=Reporter(echo=False,
                                                event_log=self.event_log),
                              extra_stop=self._should_stop, display=False,
                              can_drive=fan.is_admin())
            except Exception as exc:
                # Let the exception reach the framework: it turns the
                # service into "stopped with an error", which is what makes
                # the configured failure actions restart it.
                self.event_log.report(EVT_ERROR, "fanguard service failed: "
                                      "%s: %s" % (type(exc).__name__, exc),
                                      SEVERITY_ERROR)
                raise
            if code:
                # Same reason: only a clean stop may exit with zero, or the
                # failure actions would never fire.
                self.event_log.report(EVT_ERROR, "fanguard service stopped "
                                      "with error %d" % code, SEVERITY_ERROR)
                raise RuntimeError("the governor stopped with code %d" % code)
            self.event_log.report(EVT_STOPPED, "fanguard service stopped")
            return code

        def SvcStop(self):
            # `net stop` / `sc stop`: the governor notices within a fraction
            # of a second, hands the fans back to AUTO and exits.
            self.event_log.report(EVT_INFO, "stop requested")
            self.stop_requested = True

        def SvcShutdown(self):
            # The machine is going down: same path, so the fans end up on
            # the firmware curve.
            self.stop_requested = True

        def _should_stop(self):
            return self.stop_requested

    return _Service


SERVICE_ARGS = None       # the parsed governor options, set before dispatch


def run_as_service() -> int:
    """Hand over to the Windows service control manager."""
    import servicemanager

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(_build_service_class())
    servicemanager.StartServiceCtrlDispatcher()
    return 0


def _service_paths(options):
    """Make a relative --log path absolute.

    The SCM starts the service with the system directory as the working
    directory, so a relative path would end up in C:\\Windows\\System32
    instead of next to the tool.
    """
    out = []
    expect_path = False
    for arg in options:
        if expect_path:
            expect_path = False
            if not os.path.isabs(arg):
                arg = os.path.join(HERE, arg)
            out.append(arg)
            continue
        if arg == "--log":
            expect_path = True
            out.append(arg)
            continue
        if arg.startswith("--log="):
            value = arg.split("=", 1)[1]
            if value and not os.path.isabs(value):
                arg = "--log=" + os.path.join(HERE, value)
        out.append(arg)
    return out


def service_command_line(options=None) -> str:
    """The command line the service is started with.

    pythonw.exe, so the service never has a console window; the script
    path is absolute, because the SCM starts the service with the system
    directory as the working directory.

    The options are quoted by list2cmdline, which does it the way the SCM
    splits it again: a naive "wrap it in quotes" would turn a path that
    ends in a backslash into a different path.
    """
    import subprocess

    interpreter = sys.executable
    if interpreter.lower().endswith("python.exe"):
        windowless = os.path.join(os.path.dirname(interpreter), "pythonw.exe")
        if os.path.isfile(windowless):
            interpreter = windowless
    script = os.path.abspath(__file__)
    return subprocess.list2cmdline(
        [interpreter, script, "--service"] + _service_paths(options or []))


def _service_options(argv):
    """The governor options to hand over to the service.

    Everything the user asked for, minus the flags that only concern the
    process doing the installing and the ones the elevating parent adds to
    its child.  argparse cannot do this: it swallows the options it knows
    about, and the service is the one that has to receive them.
    """
    internal = ("--no-elevate", "--ansi")
    manage = ("--service", "--install-service", "--uninstall-service",
              "--start-service", "--stop-service")
    # A one-shot diagnostic: a service started with it would print the
    # sensor list and exit again, leaving a service that does nothing.
    oneshot = ("--list-sensors",)
    options = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in internal or arg in manage or arg in oneshot:
            continue
        if arg in ("--elev-log", "--parent-pid"):
            skip = True                 # and its value
            continue
        if arg.startswith("--elev-log=") or arg.startswith("--parent-pid="):
            continue
        options.append(arg)
    return options


def install_service(options=None) -> int:
    """Register the governor as a Windows service.  Needs Administrator."""
    import win32service
    import win32serviceutil
    import winerror

    command = service_command_line(options)
    hscm = win32service.OpenSCManager(None, None,
                                      win32service.SC_MANAGER_ALL_ACCESS)
    created = False
    try:
        try:
            handle = win32service.CreateService(
                hscm, SERVICE_NAME, SERVICE_DISPLAY_NAME,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                command, None, 0, None, None, None)
            created = True
        except win32service.error as exc:
            if exc.winerror != winerror.ERROR_SERVICE_EXISTS:
                raise
            # Already there: refresh the configuration instead.
            handle = win32service.OpenService(
                hscm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            win32service.ChangeServiceConfig(
                handle, win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                command, None, 0, None, None, None, SERVICE_DISPLAY_NAME)
        try:
            win32service.ChangeServiceConfig2(
                handle, win32service.SERVICE_CONFIG_DESCRIPTION,
                SERVICE_DESCRIPTION)
        except Exception:
            pass
        win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(hscm)

    # The event source, so the service can report what it is doing.
    EventLog().register()

    # Restart the service if it ever dies: a governor that silently stops
    # would leave the fans wherever they were.
    try:
        import subprocess
        for arguments in (["failure", SERVICE_NAME, "reset=", "86400",
                           "actions=", "restart/5000/restart/10000/restart/30000"],
                          ["failureflag", SERVICE_NAME, "1"]):
            result = subprocess.run(["sc.exe"] + arguments, check=False,
                                    capture_output=True, text=True)
            if result.returncode:
                print("  could not set the failure actions: %s"
                      % (result.stderr or result.stdout).strip())
    except Exception as exc:
        print("  could not set the failure actions: %s" % exc)

    print("service %s %s" % (SERVICE_NAME,
                             "installed" if created else "updated"))
    print("  start type : automatic (comes up with the machine)")
    print("  account    : LocalSystem")
    print("  command    : %s" % command)

    # A running instance still uses the old command line, so restart it.
    try:
        hscm = win32service.OpenSCManager(None, None,
                                          win32service.SC_MANAGER_CONNECT)
        try:
            handle = win32service.OpenService(
                hscm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        except Exception:
            handle = None
        if handle is not None:
            try:
                running = (win32service.QueryServiceStatus(handle)[1]
                            == win32service.SERVICE_RUNNING)
            finally:
                win32service.CloseServiceHandle(handle)
            if running:
                print("  restarting it so the new settings take effect ...")
                win32serviceutil.StopService(SERVICE_NAME)
                win32serviceutil.WaitForServiceStatus(
                    SERVICE_NAME, win32service.SERVICE_STOPPED, 30)
        win32service.CloseServiceHandle(hscm)
    except Exception as exc:
        print("  could not check the service state: %s" % exc)

    try:
        win32serviceutil.StartService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_RUNNING, 30)
        print("  started")
    except win32service.error as exc:
        if exc.winerror != winerror.ERROR_SERVICE_ALREADY_RUNNING:
            print("  could not start it: %s" % exc.strerror)
            return fan.EXIT_ERROR
        print("  already running")
    except Exception as exc:
        print("  it did not come up: %s" % exc)
        print("  look in the event log (Application -> %s)" % EVENT_SOURCE)
        return fan.EXIT_ERROR
    return fan.EXIT_OK


def uninstall_service() -> int:
    """Remove the service again.  Needs Administrator."""
    import win32service
    import win32serviceutil
    import winerror

    hscm = win32service.OpenSCManager(None, None,
                                      win32service.SC_MANAGER_ALL_ACCESS)
    try:
        try:
            handle = win32service.OpenService(
                hscm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        except win32service.error as exc:
            if exc.winerror == winerror.ERROR_SERVICE_DOES_NOT_EXIST:
                print("service %s is not installed" % SERVICE_NAME)
                return fan.EXIT_OK
            raise

        status = win32service.QueryServiceStatus(handle)[1]
        if status != win32service.SERVICE_STOPPED:
            print("stopping the service ...")
            try:
                win32serviceutil.StopService(SERVICE_NAME)
                win32serviceutil.WaitForServiceStatus(
                    SERVICE_NAME, win32service.SERVICE_STOPPED, 30)
            except Exception as exc:
                print("  could not stop it: %s" % exc)
                win32service.CloseServiceHandle(handle)
                return fan.EXIT_ERROR
        win32service.DeleteService(handle)
        win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(hscm)

    try:
        import win32evtlogutil
        win32evtlogutil.RemoveSourceFromRegistry(EVENT_SOURCE)
    except Exception:
        pass

    print("service %s removed" % SERVICE_NAME)
    print("  the fans are back on the firmware AUTO curve")
    return fan.EXIT_OK


def start_service() -> int:
    """Start the installed service.  Needs Administrator."""
    import win32service
    import winerror
    import win32serviceutil

    try:
        win32serviceutil.StartService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_RUNNING, 30)
    except win32service.error as exc:
        if exc.winerror == winerror.ERROR_SERVICE_ALREADY_RUNNING:
            print("service %s is already running" % SERVICE_NAME)
            return fan.EXIT_OK
        print("could not start it: %s" % exc.strerror)
        return fan.EXIT_ERROR
    print("service %s started" % SERVICE_NAME)
    return fan.EXIT_OK


def stop_service() -> int:
    """Stop the installed service.  Needs Administrator.

    The service hands the fans back to AUTO on its way out, so this is the
    clean way to give the fans back to the firmware.
    """
    import win32service
    import winerror
    import win32serviceutil

    hscm = win32service.OpenSCManager(None, None,
                                      win32service.SC_MANAGER_ALL_ACCESS)
    try:
        try:
            handle = win32service.OpenService(
                hscm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        except win32service.error as exc:
            if exc.winerror == winerror.ERROR_SERVICE_DOES_NOT_EXIST:
                print("service %s is not installed" % SERVICE_NAME)
                return fan.EXIT_OK
            raise
        win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(hscm)

    try:
        win32serviceutil.StopService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_STOPPED, 30)
    except win32service.error as exc:
        if exc.winerror == winerror.ERROR_SERVICE_NOT_ACTIVE:
            print("service %s is not running" % SERVICE_NAME)
            return fan.EXIT_OK
        print("could not stop it: %s" % exc.strerror)
        return fan.EXIT_ERROR
    except Exception as exc:
        print("could not stop it: %s" % exc)
        return fan.EXIT_ERROR
    print("service %s stopped -- the fans are back on AUTO"
          % SERVICE_NAME)
    return fan.EXIT_OK


def govern(args, drives_fans, reporter, extra_stop=None, display=True,
           can_drive=False):
    """The governor proper: sensors, fan interface, control loop.

    Shared by the foreground mode and the service, so both take the
    single-instance lock, both restore AUTO on the way out, and neither
    starts twice.
    """
    # Only the process that actually drives the fans may take the lock.  A
    # non-elevated launcher would otherwise grab it and the elevated child
    # would then wrongly conclude that another instance is running.
    # Read-only modes (--list-sensors, --dry-run) never touch the fans, so
    # they must not be blocked by a running governor.
    lock = None
    if drives_fans and can_drive:
        lock, message = fan.acquire_single_instance()
        if lock is None:
            reporter.say("ERROR: %s" % message, SEVERITY_ERROR)
            reporter.say("Stop the running instance first "
                         "(net stop %s)." % SERVICE_NAME, SEVERITY_ERROR)
            return fan.EXIT_ERROR
        reporter.say("  %s" % message)

    thermo = Thermometer()
    fan_iface = None
    log = Log(args.log)
    if display:
        # Closing the console window must not leave the fans pinned: ask
        # Windows to tell us, so we can shut down in an orderly way.
        fan.install_console_handler()
    try:
        thermo.start()
        reporter.say("LibreHardwareMonitor ready")
        reporter.say("  sensors: %s" % thermo.describe())
        reporter.say("  CPU fan   : AUTO below %d C, MAX at %d C"
                     % (args.auto_below, args.cpu_max_at))
        reporter.say("  memory fan: AUTO below %d C, MAX at %d C"
                     % (args.auto_below, args.mem_max_at))

        if args.list_sensors:
            for hw in thermo._computer.Hardware:
                hw.Update()
                for s in hw.Sensors:
                    if str(s.SensorType) == "Temperature":
                        reporter.say("  %-14s %-34s = %s"
                                     % (str(hw.HardwareType), str(s.Name),
                                        _value(s)))
            return 0

        if not args.dry_run:
            fan_iface = fan.FanInterface()
        # Only the instance that really owns the fans may publish: a
        # side process without the lock would otherwise overwrite the
        # governor's samples on the bus.
        return run(args, thermo, fan_iface, log,
                   extra_stop=extra_stop, display=display,
                   publish=bool(lock) and drives_fans)

    except fan.FanError as exc:
        reporter.say("ERROR: %s" % exc, SEVERITY_ERROR)
        return fan.EXIT_ERROR
    except KeyboardInterrupt:
        if display:
            print()
        return 130
    except Exception as exc:
        if os.environ.get("FANCTL_TRACEBACK"):
            raise
        reporter.say("ERROR: unexpected failure: %s: %s"
                     % (type(exc).__name__, exc), SEVERITY_ERROR)
        return fan.EXIT_ERROR
    finally:
        # Hand the fans back to the firmware curve unless told otherwise.
        # This comes BEFORE thermo.stop(): on a console close Windows only
        # allows a few seconds, and tearing down the sensor driver first
        # could hang and get us killed with the fans still pinned.
        if fan_iface is not None and not args.no_restore \
                and not args.list_sensors:
            try:
                fan_iface.set_duty(fan.FAN_CPU, fan.DUTY_AUTO)
                fan_iface.set_duty(fan.FAN_MEM, fan.DUTY_AUTO)
                reporter.say("fans returned to AUTO")
            except fan.FanError as exc:
                reporter.say("WARNING: could not restore AUTO: %s" % exc,
                             SEVERITY_WARNING)
        thermo.stop()
        log.close()
        fan.release_single_instance(lock)


def main(argv=None) -> int:
    fan.init_console()

    parser = argparse.ArgumentParser(
        prog="fanguard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Background fan governor driven by CPU / memory temperature.")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between decisions (default: %(default)s)")
    parser.add_argument("--step", type=int, default=DEFAULT_STEP,
                        help="duty %% granularity (default: %(default)s)")
    parser.add_argument("--min-duty", type=int, default=DEFAULT_MIN,
                        help="lowest duty %% (default: %(default)s)")
    parser.add_argument("--max-duty", type=int, default=DEFAULT_MAX,
                        help="highest duty %% (default: %(default)s)")
    parser.add_argument("--auto-below", type=int, default=AUTO_BELOW,
                        help="below this temp, hand the fan back to AUTO "
                             "(default: %(default)s)")
    parser.add_argument("--cpu-max-at", type=int, default=CPU_MAX_AT,
                        help="CPU fan goes MAX at this temp (default: %(default)s)")
    parser.add_argument("--mem-max-at", type=int, default=MEM_MAX_AT,
                        help="memory fan goes MAX at this temp (default: %(default)s)")
    parser.add_argument("--hysteresis", type=int, default=DEFAULT_HYST,
                        help="degrees of hysteresis around the AUTO boundary "
                             "(default: %(default)s)")

    parser.add_argument("--dry-run", action="store_true",
                        help="never touch the fans, only log what it would do")
    parser.add_argument("--log", metavar="FILE", help="append a CSV-ish log")
    parser.add_argument("--max-failures", type=int, default=5,
                        help="give up after N bad sensor reads (default: 5)")
    parser.add_argument("--duration", type=float, default=0,
                        help="stop after N seconds, 0 = run forever (default: 0)")
    parser.add_argument("--list-sensors", action="store_true",
                        help="print every temperature sensor and exit")
    parser.add_argument("--no-restore", action="store_true",
                        help="do not hand the fans back to AUTO on exit")
    parser.add_argument("--service", action="store_true",
                        help="run as a Windows service (started by the "
                             "service control manager)")
    parser.add_argument("--install-service", action="store_true",
                        help="register the Windows service and start it "
                             "(Administrator)")
    parser.add_argument("--uninstall-service", action="store_true",
                        help="stop and remove the Windows service "
                             "(Administrator)")
    parser.add_argument("--start-service", action="store_true",
                        help="start the Windows service (Administrator)")
    parser.add_argument("--stop-service", action="store_true",
                        help="stop the Windows service (Administrator)")
    parser.add_argument("--ansi", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--no-elevate", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--elev-log", metavar="PATH",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", metavar="PID", type=int,
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    argv = list(sys.argv[1:] if argv is None else argv)
    service_mode = any(flag in argv for flag in
                       ("--service", "--install-service", "--uninstall-service",
                        "--start-service", "--stop-service"))
    # The options for the service come from the raw command line, not from
    # argparse: it consumes the ones it knows about, and the service is the
    # one that has to receive them.
    options = _service_options(argv) if service_mode else []
    args = parser.parse_args(argv)

    elev_log = getattr(args, "elev_log", None)
    if elev_log:
        fan.install_elev_log(elev_log)

    # -- service plumbing ---------------------------------------------------
    # These do not need the governor at all, and the installer has to work
    # before the service exists.
    if (args.install_service or args.uninstall_service
            or args.start_service or args.stop_service):
        if not fan.is_admin():
            return fan.ensure_admin(
                reason="Managing a Windows service needs Administrator.")
        if args.install_service:
            return install_service(options)
        if args.uninstall_service:
            return uninstall_service()
        if args.start_service:
            return start_service()
        return stop_service()

    if args.service:
        # Started by the service control manager: it runs as LocalSystem,
        # so it is already administrator and must not try to elevate.
        global SERVICE_ARGS
        SERVICE_ARGS = args
        return run_as_service()

    if not elev_log and not getattr(args, "no_elevate", False):
        if not fan.is_admin():
            # elevate_stream(), not ensure_admin(): fanguard runs for a long
            # time, so the child's output has to be relayed live rather than
            # dumped in one go when it finally exits.
            #
            # Unattended (pythonw without a console) nobody is going to read
            # that output, so it is thrown away instead of being written to
            # a relay file for the whole life of the run.
            print("fanguard needs Administrator to read temperatures and "
                  "drive the fans.")
            print("Requesting elevation...")
            return fan.elevate_stream(quiet=QUIET)

    # The governor may only touch the fans when it is actually allowed to
    # (administrator rights); govern() takes the single-instance lock.
    drives_fans = not args.dry_run and not args.list_sensors
    can_drive = bool(elev_log) or fan.is_admin()

    return govern(args, drives_fans, Reporter(), can_drive=can_drive)


if __name__ == "__main__":
    sys.exit(main())