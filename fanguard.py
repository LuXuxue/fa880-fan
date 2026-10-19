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

Safety: on exit -- normally, on Ctrl+C, or when the console window is
closed -- the fans are handed back to the firmware AUTO curve, unless
--no-restore is given.  A hard kill (taskkill /F) cannot be caught and
does skip that.
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

# Where a stop request is left.  fanlib owns it so the elevating launcher
# can ask its child to shut down cleanly too.
STOP_FILE = fan.STOP_FILE

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

    A file is only written when --log is given explicitly: an unattended
    fanguard-start should not grow a log file forever, and touching the disk
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


def run(args, thermo, fan_iface, log, drives_fans=True):
    cpu = Governor("CPU fan", args.auto_below, args.cpu_max_at,
                   args.hysteresis, args.min_duty,
                   args.max_duty, args.step)
    mem = Governor("Memory fan", args.auto_below, args.mem_max_at,
                   args.hysteresis, args.min_duty,
                   args.max_duty, args.step)

    writer = fan.FrameWriter(getattr(args, "ansi", False)
                             or sys.stdout.isatty())
    parent = fan.parent_handle(getattr(args, "parent_pid", None))
    started = time.time()

    # Publish each tick into shared memory so a non-elevated fanspeed can
    # display live speeds without WMI access of its own.
    bus = fan.TelemetryBus()
    if not bus.open_writer():
        bus = None

    failures = 0
    last = {"cpu": None, "mem": None}
    decided = {"cpu": None, "mem": None}   # dry-run bookkeeping only
    next_tick = started
    deadline = started + args.duration if args.duration > 0 else None

    try:
        while not fan.parent_gone(parent):
            if fan.close_requested():
                # The window was closed: shut down in an orderly way so the
                # finally block can hand the fans back to AUTO.
                writer.banner("window closed, shutting down")
                break
            if drives_fans and fan.stop_requested():
                # Asked to stop: clean up and leave AUTO in charge.  Only the
                # instance that actually drives the fans may act on this --
                # a --dry-run must not swallow the request meant for it.
                try:
                    os.remove(STOP_FILE)
                except OSError:
                    pass
                writer.banner("stop requested, shutting down")
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
                break

            changed = False          # per-tick: only write the state file
                                    # when a duty actually changed
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
                changed = True

            if not args.dry_run and changed:
                if last["cpu"] is not None or last["mem"] is not None:
                    fan.write_state(last["cpu"], last["mem"], "fanguard")

            rpms = None
            if not args.dry_run:
                try:
                    rpms = fan_iface.get_rpm()
                except fan.FanError:
                    rpms = None
            log.row(time.strftime("%Y-%m-%d %H:%M:%S"), temps, cpu, mem,
                    rpms, args.dry_run)
            if bus is not None and not args.dry_run:
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

    return 0


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
    parser.add_argument("--ansi", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--no-elevate", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--elev-log", metavar="PATH",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", metavar="PID", type=int,
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    elev_log = getattr(args, "elev_log", None)
    if elev_log:
        fan.install_elev_log(elev_log)

    if not elev_log and not getattr(args, "no_elevate", False):
        if not fan.is_admin():
            # elevate_stream(), not ensure_admin(): fanguard runs for a long
            # time, so the child's output has to be relayed live rather than
            # dumped in one go when it finally exits.
            print("fanguard needs Administrator to read temperatures and "
                  "drive the fans.")
            print("Requesting elevation...")
            return fan.elevate_stream()

    # Only the process that actually drives the fans may take the lock.  A
    # non-elevated launcher would otherwise grab it and the elevated child
    # would then wrongly conclude that another instance is running.
    # Read-only modes (--list-sensors, --dry-run) never touch the fans, so
    # they must not be blocked by a running governor.
    lock = None
    drives_fans = not args.dry_run and not args.list_sensors
    if drives_fans and (elev_log or fan.is_admin()):
        lock, message = fan.acquire_single_instance()
        if lock is None:
            print("ERROR: %s" % message, file=sys.stderr)
            print("Use fanguard-stop.bat to stop the running instance.",
                  file=sys.stderr)
            return fan.EXIT_ERROR
        print("  %s" % message)

        # Only the instance that owns the governor may clear a stop request:
        # a --dry-run must never eat the request meant for the real one.
        # Do it here, before the sensor driver loads, so the window in which
        # a fresh request could be discarded is as small as possible.
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass

    thermo = Thermometer()
    fan_iface = None
    log = Log(args.log)
    # Closing the console window must not leave the fans pinned: ask Windows
    # to tell us, so we can shut down in an orderly way.
    fan.install_console_handler()
    try:
        thermo.start()
        print("LibreHardwareMonitor ready")
        print("  sensors: %s" % thermo.describe())
        print("  CPU fan   : AUTO below %d C, MAX at %d C"
              % (args.auto_below, args.cpu_max_at))
        print("  memory fan: AUTO below %d C, MAX at %d C"
              % (args.auto_below, args.mem_max_at))

        if args.list_sensors:
            for hw in thermo._computer.Hardware:
                hw.Update()
                for s in hw.Sensors:
                    if str(s.SensorType) == "Temperature":
                        print("  %-14s %-34s = %s"
                              % (str(hw.HardwareType), str(s.Name),
                                 _value(s)))
            return 0

        if not args.dry_run:
            fan_iface = fan.FanInterface()
        return run(args, thermo, fan_iface, log, drives_fans)

    except fan.FanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ERROR
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        if os.environ.get("FANCTL_TRACEBACK"):
            raise
        print("ERROR: unexpected failure: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
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
                fan.write_state(fan.DUTY_AUTO, fan.DUTY_AUTO, "fanguard")
                print("fans returned to AUTO")
            except fan.FanError as exc:
                print("WARNING: could not restore AUTO: %s" % exc,
                      file=sys.stderr)
        thermo.stop()
        log.close()
        fan.release_single_instance(lock)


if __name__ == "__main__":
    sys.exit(main())