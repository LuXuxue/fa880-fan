#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fanspeed -- live view of CPU / memory fan speed.

    fanspeed            live display
    fanspeed --status   one-line "is fanguard running?" check, then exit

Refreshes once a second and keeps redrawing until you close the window or
press Ctrl+C.  Read-only: it never touches the fan settings.  Use fanctl.py
to change them.

Elevation is needed only if it has to read the WMI interface itself:

  * if fanguard is running, fanspeed reads its samples from shared memory and
    needs no privileges at all;
  * otherwise it asks for elevation (the ACPI PNP0C14 methods are refused to
    unprivileged callers, reads included).

--status never elevates: it only asks the shared-memory bus, and exits 0 when
a governor is running, 1 when it is not.

fanguard stopping mid-session is handled: fanspeed falls back to WMI.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

import fanlib as fan

INTERVAL = 1.0


def _num(value, cast=int, default=None):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _gear(label):
    """'AUTO' -> 101, 'MAX' -> 100, '70%' -> 70, else None."""
    if label is None:
        return None
    label = label.strip()
    if label.upper() == "AUTO":
        return fan.DUTY_AUTO
    if label.upper() == "MAX":
        return fan.DUTY_MAX
    return _num(label.rstrip("%"))


# --------------------------------------------------------------------------
# source 1: shared memory fed by a running fanguard (no elevation)
# --------------------------------------------------------------------------

def render_sample(sample, started, interval) -> str:
    cpu_rpm = _num(sample.get("cpu_rpm")) or 0
    mem_rpm = _num(sample.get("mem_rpm")) or 0
    state = {"cpu": _gear(sample.get("duty_cpu")),
             "mem": _gear(sample.get("duty_mem"))}
    wall = _num(sample.get("wall"), float)

    lines = [
        "",
        "  Fan speed   %s   (+%ds, via fanguard pid %s)"
        % (time.strftime("%H:%M:%S"), int(time.time() - started),
           sample.get("pid", "?")),
        "",
    ]
    lines += fan.fan_rows(cpu_rpm, mem_rpm, state)
    lines += [
        "",
        "  temperatures: CPU %s C   memory %s C   (from fanguard)"
        % (sample.get("cpu", "?"), sample.get("mem", "?")),
        "  sample age  : %s"
        % ("%.1fs" % (time.time() - wall) if wall else "?"),
        "  gauges scaled to each fan's own MAX -- refresh %gs,"
        % interval,
        "  close the window or press Ctrl+C to exit",
    ]
    return "\n".join(lines)


def watch_bus(args, bus, first, started) -> int:
    writer = fan.FrameWriter(getattr(args, "ansi", False)
                             or sys.stdout.isatty())
    parent = fan.parent_handle(getattr(args, "parent_pid", None))
    writer.banner("Following fanguard via shared memory -- "
                  "no elevation needed.")
    sample = first
    try:
        while not fan.parent_gone(parent):
            writer.write(render_sample(sample, started, args.interval))
            time.sleep(args.interval)

            fresh = bus.read()
            if fresh is not None:
                sample = fresh
            elif not fan.TelemetryBus.is_live(sample, args.max_age):
                # fanguard died or stopped publishing: fall back to WMI.
                writer.banner("fanguard is no longer publishing -- "
                              "switching to WMI (needs elevation)")
                bus.close()
                if not fan.is_admin():
                    return fan.elevate_stream()
                return watch(args)
    finally:
        if parent:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(parent)
            except Exception:
                pass
        bus.close()
    return 0


# --------------------------------------------------------------------------
# source 2: the WMI interface directly (needs Administrator)
# --------------------------------------------------------------------------

def render(fan_iface, started, interval) -> str:
    """Build one frame of plain text.  FrameWriter handles the redrawing."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        fan1, fan2 = fan_iface.get_rpm()
        error = None
    except fan.FanError as exc:
        fan1 = fan2 = 0
        error = str(exc)

    elapsed = int(time.time() - started)
    state = fan.read_state()          # read once per frame, not twice
    lines = [
        "",
        "  Fan speed   %s   (+%ds, via WMI)" % (now, elapsed),
        "  instance: %s" % fan_iface.instance,
        "",
    ]
    lines += fan.fan_rows(fan1, fan2, state)
    lines += [
        "",
        "  raw FanDuty: 0x%08X   (fan1 = low 16 bits, fan2 = high 16)"
        % fan_iface.last_packed,
        fan.state_note(state),
    ]
    if error:
        lines.append("  ERROR: %s" % error)

    lines += [
        "",
        "  gauges scaled to each fan's own MAX -- refresh %gs,"
        % interval,
        "  close the window or press Ctrl+C to exit",
    ]
    return "\n".join(lines)


def watch(args) -> int:
    fan_iface = fan.FanInterface()
    started = time.time()
    # --ansi is only passed by the elevating parent, and its default is
    # argparse.SUPPRESS, so it may be absent entirely.  Fall back to
    # isatty(): when the user runs fanspeed directly in a real console we
    # still want the in-place redraw, not a screenful of scrollback.
    writer = fan.FrameWriter(getattr(args, "ansi", False)
                             or sys.stdout.isatty())
    # When elevated from a non-admin launcher we are a hidden child process
    # with a console of our own; closing the user's window kills the parent
    # but sends us no notification.  Watch the parent so we exit with it
    # instead of being orphaned.
    parent = fan.parent_handle(getattr(args, "parent_pid", None))
    writer.banner("Watching fan speed (WMI). Close this window or press "
                  "Ctrl+C to exit.")
    try:
        while not fan.parent_gone(parent):
            writer.write(render(fan_iface, started, args.interval))
            time.sleep(args.interval)
    finally:
        if parent:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(parent)
            except Exception:
                pass
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _stop():
    """Leave the terminal in a sane state after Ctrl+C."""
    sys.stdout.write("\x1b[0m\n")
    sys.stdout.flush()


def cmd_status(args) -> int:
    """Report whether a fanguard instance is running.

    Two independent signals:
      * the single-instance mutex -- authoritative: somebody holds the lock,
        so a governor owns the fans even if it has stopped publishing;
      * the shared-memory bus -- tells us it is alive AND publishing, and
        carries the pid and the last sample.

    The bus is a kernel object: it disappears with the last handle, so it
    cannot go stale the way a lock file on disk can.  A sample older than
    --max-age counts as "not publishing" even if the mapping is still around.

    Exit code 0 = a governor is running, 1 = none is.
    """
    present = fan.governor_present()

    bus = fan.TelemetryBus()
    sample = None
    try:
        if bus.open_reader():
            sample = bus.read()
    finally:
        bus.close()

    live = fan.TelemetryBus.is_live(sample, args.max_age)

    if not present and not live:
        print("not running")
        return 1
    if live:
        wall = _num(sample.get("wall"), float)
        age = ("%.1fs" % (time.time() - wall)) if wall else "?"
        print("running  pid=%s  age=%s  cpu=%s C  mem=%s C"
              % (sample.get("pid", "?"), age, sample.get("cpu", "?"),
                 sample.get("mem", "?")))
    elif sample is not None:
        # Holds the lock and published before, but not lately: alive but
        # wedged (hung sensor driver, stuck WMI call).
        wall = _num(sample.get("wall"), float)
        age = ("%.1fs" % (time.time() - wall)) if wall else "?"
        print("running  (holds the lock, but its last sample is %s old)"
              % age)
    else:
        # Holds the lock but has never published: still starting up.
        print("running  (holds the lock, no sample published yet)")
    return 0


def main(argv=None) -> int:
    fan.init_console()

    parser = argparse.ArgumentParser(
        prog="fanspeed",
        description="Live-updating CPU / memory fan speed display.")
    parser.add_argument("--interval", type=float, default=INTERVAL,
                        help="refresh period in seconds (default: %(default)s)")
    parser.add_argument("--max-age", type=float, default=fan.TELEMETRY_MAX_AGE,
                        help="how old a fanguard sample may be before we "
                             "stop trusting it (default: %g)"
                             % fan.TELEMETRY_MAX_AGE)
    parser.add_argument("--force-wmi", action="store_true",
                        help="ignore fanguard and read WMI directly")
    parser.add_argument("--status", action="store_true",
                        help="print whether a fanguard instance is running "
                             "and exit (no elevation, no display)")
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

    # --status is a pure shared-memory query: it must never ask for elevation
    # and must never start a display.
    if args.status:
        return cmd_status(args)

    # Prefer the shared-memory feed: one mapping, opened once, so no handle
    # churn while we loop.
    if not args.force_wmi and not elev_log:
        bus = fan.TelemetryBus()
        if bus.open_reader():
            sample = bus.read()
            if fan.TelemetryBus.is_live(sample, args.max_age):
                try:
                    return watch_bus(args, bus, sample, time.time())
                except KeyboardInterrupt:
                    _stop()
                    return 130
            bus.close()

    if not elev_log and not getattr(args, "no_elevate", False):
        if not fan.is_admin():
            print("fanguard is not running, and reading the fan interface "
                  "needs Administrator.")
            print("Requesting elevation...")
            return fan.elevate_stream()

    try:
        return watch(args)
    except KeyboardInterrupt:
        _stop()
        return 130
    except fan.AccessDenied as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ACCESS
    except fan.FanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ERROR
    except Exception as exc:
        if os.environ.get("FANCTL_TRACEBACK"):
            raise
        print("ERROR: unexpected failure: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return fan.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())