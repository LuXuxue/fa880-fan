#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fanctl -- set CPU / memory fan speed independently from the command line.

    fanctl                       report current state, change nothing
    fanctl --cpu max             set the CPU fan only
    fanctl --mem auto            set the memory (GPU) fan only
    fanctl --cpu auto --mem max  set each fan separately
    fanctl --dump                print the raw WMI method signatures

Modes:  auto (=101, firmware curve)   max (=100)   35..100 = manual duty %

Anything you do not mention is left exactly as it is.  With neither --cpu nor
--mem, fanctl only reports and never writes.

See fanlib.py for the API details, or fanspeed.py for a live-updating view.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import fanlib as fan


def apply(fan_iface, targets, settle=1.5):
    """targets: list of (fan_number, duty).

    Each fan gets its own SetFanControl call, so the other fan keeps its mode.
    Nothing is written to disk: the gear that is now in effect is published
    by the running governor on the shared-memory bus, and a one-shot change
    like this one has nothing to keep alive afterwards.
    Returns True when everything went through.
    """
    failed = False
    for number, duty in targets:
        try:
            status = fan_iface.set_duty(number, duty)
        except fan.FanError as exc:
            print("  fan%d  %-22s FAILED: %s" % (number, fan.describe(duty), exc))
            failed = True
            continue
        if status == 0:
            print("  fan%d  %-22s ok" % (number, fan.describe(duty)))
        else:
            print("  fan%d  %-22s firmware rejected it (ResultStatus=%d)"
                  % (number, fan.describe(duty), status))
            failed = True

    if settle > 0:
        time.sleep(settle)
        try:
            fan1, fan2 = fan_iface.get_rpm()
            print("  ->  CPU %s, memory %s"
                  % (fan.format_rpm(fan1), fan.format_rpm(fan2)))
        except fan.FanError:
            pass
    return not failed


def main(argv=None) -> int:
    fan.init_console()

    parser = argparse.ArgumentParser(
        prog="fanctl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Independent CPU / memory fan control via ACPI PNP0C14 WMI.",
        epilog="modes:  auto (=101, firmware)   max (=100)   "
               "35..100 = manual duty %")
    parser.add_argument("--cpu", metavar="MODE", type=fan.parse_mode,
                        help="CPU fan: auto | max | 35..100")
    parser.add_argument("--mem", metavar="MODE",
                        type=fan.parse_mode,
                        help="memory fan: auto | max | 35..100")
    parser.add_argument("--dump", action="store_true",
                        help="print the raw WMI method signatures")
    parser.add_argument("--no-elevate", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--elev-log", metavar="PATH",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    # Accepted but unused: the elevating parent always passes --parent-pid to
    # the child, so the parser has to know the option.  fanctl is a one-shot
    # command, so it has no long-running child to watch out for.
    parser.add_argument("--parent-pid", metavar="PID", type=int,
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    elev_log = getattr(args, "elev_log", None)
    if elev_log:
        fan.install_elev_log(elev_log)

    if not elev_log and not getattr(args, "no_elevate", False):
        rc = fan.ensure_admin()
        if rc is not None:
            return rc

    try:
        fan_iface = fan.FanInterface()
    except fan.AccessDenied as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ACCESS
    except fan.FanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ERROR

    try:
        if args.dump:
            return fan_iface.dump()

        targets = []
        if args.cpu is not None:
            targets.append((fan.FAN_CPU, args.cpu))
        if args.mem is not None:
            targets.append((fan.FAN_MEM, args.mem))

        if not targets:
            # Nothing requested: report only, leave both fans untouched.
            fan.report(fan_iface)
            return fan.EXIT_OK

        if fan.service_running():
            # The governor drives the fans itself; whatever is set here is
            # overwritten within a tick.
            print("  NOTE: the %s service is running and will overwrite "
                  "this within a few seconds." % fan.SERVICE_NAME)
            print("        stop it first:  net stop %s" % fan.SERVICE_NAME)

        plan = ", ".join("%s -> %s" % (fan.FAN_LABEL[n], fan.describe(d))
                         for n, d in targets)
        fan.title("Applying  (%s)" % plan)
        return fan.EXIT_OK if apply(fan_iface, targets) else fan.EXIT_ERROR

    except fan.AccessDenied as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ACCESS
    except fan.FanError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return fan.EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if os.environ.get("FANCTL_TRACEBACK"):
            raise
        print("ERROR: unexpected failure: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return fan.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())