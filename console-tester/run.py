#!/usr/bin/env python3
"""run.py: run a PS2Recomp port with a hard timeout and judge the run (console-tester).

`boot` starts the runner (ps2EntryRunner <ELF>), captures stdout and stderr into
console/runs/<UTC stamp>.log (the first and last 2000 lines when huge), kills the
whole process tree on timeout, and reports PASS, HANG, CRASH or FAIL, writing
console/runs/latest.json. `--input` sets PS2X_INPUT_SCRIPT for the controller
replay built by /console-controller. `soak` repeats a boot N times and sums up.

Python 3.8+ standard library only.
"""

import argparse
import collections
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

FATAL_MARKERS = (
    "Error during program execution:",
    "[main] fatal exception",
    "[terminate]",
    "Failed to initialize PS2 runtime",
    "Failed to initialize PS2 memory",
    "Failed to load ELF file",
    "Failed to open ELF file",
)
ISSUE_MARKERS = {
    "stub": "Warning: Unimplemented PS2 stub called",
    "syscall": "Warning: Unimplemented PS2 syscall called",
    "missing": "Error: No exact recompiled function",
    "missing_target": "[guest-branch:missing-target]",
    "iop": "[IOP/RPC trace:unhandled]",
}
RESULTS = ("PASS", "HANG", "CRASH", "FAIL")


class UsageError(Exception):
    pass


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique_log_path(out_dir, stamp):
    path = os.path.join(out_dir, stamp + ".log")
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s-%d.log" % (stamp, n))
        n += 1
    return path


def kill_tree(proc, grace=2.0):
    """Stop the runner and everything it started."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + grace
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_name(code):
    try:
        return signal.Signals(-code).name
    except (ValueError, AttributeError):
        return "signal %d" % -code


class Capture:
    """Bounded log capture: first `keep` lines, last `keep` lines, counts, expectations."""

    def __init__(self, keep, expects):
        self.keep = keep
        self.head = []
        self.tail = collections.deque(maxlen=keep)
        self.total = 0
        self.expects = [(pattern, re.compile(pattern)) for pattern in expects]
        self.seen = {pattern: None for pattern in expects}
        self.first_fatal = None
        self.first_issue = None
        self.counts = {k: 0 for k in ISSUE_MARKERS}
        self.replay_loaded = None
        self.replay_problem = None
        self.last_output = time.monotonic()
        self.lock = threading.Lock()
        self.all_seen = threading.Event()
        if not self.expects:
            self.all_seen.set()

    def add(self, line):
        with self.lock:
            self.total += 1
            number = self.total
            if len(self.head) < self.keep:
                self.head.append(line)
            else:
                self.tail.append(line)
            self.last_output = time.monotonic()
            for pattern, rx in self.expects:
                if self.seen[pattern] is None and rx.search(line):
                    self.seen[pattern] = {"line": number, "text": line[:300]}
            if self.expects and all(v is not None for v in self.seen.values()):
                self.all_seen.set()
            if self.first_fatal is None and any(m in line for m in FATAL_MARKERS):
                self.first_fatal = {"line": number, "text": line[:300]}
            for kind, marker in ISSUE_MARKERS.items():
                if marker in line:
                    self.counts[kind] += 1
                    if self.first_issue is None:
                        self.first_issue = {"line": number, "kind": kind, "text": line[:300]}
            if "[pad-replay] loaded" in line:
                self.replay_loaded = line.strip()
            elif "[pad-replay]" in line and ("replay disabled" in line or "cannot open" in line):
                self.replay_problem = line.strip()

    def lines(self):
        omitted = self.total - len(self.head) - len(self.tail)
        out = list(self.head)
        if omitted > 0:
            out.append("[run.py] ... %d lines omitted (kept the first %d and the last %d) ..."
                       % (omitted, len(self.head), len(self.tail)))
        out.extend(self.tail)
        return out, max(omitted, 0)


def _reader(stream, capture):
    try:
        for raw in iter(stream.readline, b""):
            capture.add(raw.decode("utf-8", "replace").rstrip("\r\n"))
    except (OSError, ValueError):
        pass


def classify(timed_out, stopped_on_pass, returncode, capture, expects_given):
    """Return (result, reason)."""
    ended_itself = not (timed_out or stopped_on_pass)
    all_seen = all(v is not None for v in capture.seen.values())
    if capture.first_fatal is not None:
        return "CRASH", "fatal line %d: %s" % (capture.first_fatal["line"], capture.first_fatal["text"])
    if ended_itself and returncode is not None and returncode != 0:
        if returncode < 0:
            return "CRASH", "killed by %s" % _signal_name(returncode)
        return "CRASH", "exited with code %d" % returncode
    if ended_itself and (capture.counts["missing"] or capture.counts["missing_target"]):
        return "CRASH", "stopped after jumping to a function the recompiler never emitted"
    if expects_given and all_seen:
        return "PASS", "every expected line appeared"
    if not expects_given and not ended_itself:
        return "PASS", "ran the whole timeout without a crash"
    if timed_out:
        missing = [p for p, v in capture.seen.items() if v is None]
        return "HANG", "timeout with no crash; never saw: %s" % ", ".join(missing)
    if expects_given:
        missing = [p for p, v in capture.seen.items() if v is None]
        return "FAIL", "exited by itself (code 0) without: %s" % ", ".join(missing)
    return "FAIL", "exited by itself (code 0) before the timeout"


def run_once(runner, elf, seconds=15.0, expects=(), input_script=None, record=None,
             out_dir=os.path.join("console", "runs"), extra_args=(), cwd=None, keep=2000,
             until_expect=False, label=None):
    os.makedirs(out_dir, exist_ok=True)
    stamp = utc_stamp()
    log_path = _unique_log_path(out_dir, stamp)
    env = dict(os.environ)
    env.pop("PS2X_INPUT_SCRIPT", None)
    env.pop("PS2X_INPUT_RECORD", None)
    if input_script:
        env["PS2X_INPUT_SCRIPT"] = os.path.abspath(input_script)
    if record:
        env["PS2X_INPUT_RECORD"] = os.path.abspath(record)
    cmd = [os.path.abspath(runner), os.path.abspath(elf)] + list(extra_args)
    capture = Capture(keep, list(expects))
    popen = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                 env=env, cwd=cwd)
    if os.name == "nt":
        popen["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen["start_new_session"] = True
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, **popen)
    except OSError as exc:
        raise UsageError("cannot start %s: %s" % (runner, exc))
    reader = threading.Thread(target=_reader, args=(proc.stdout, capture), daemon=True)
    reader.start()
    deadline = started + seconds
    timed_out = stopped_on_pass = False
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        if until_expect and capture.expects and capture.all_seen.is_set():
            stopped_on_pass = True
            break
        pause = min(0.05, max(0.0, deadline - now))
        if until_expect:
            capture.all_seen.wait(pause)
        else:
            time.sleep(pause)
    if timed_out or stopped_on_pass:
        kill_tree(proc)
    returncode = proc.wait()
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # anything the runner left behind
        except (ProcessLookupError, PermissionError):
            pass
    reader.join(timeout=5)
    try:
        proc.stdout.close()
    except OSError:
        pass
    elapsed = time.monotonic() - started

    lines, omitted = capture.lines()
    with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")
    shutil.copyfile(log_path, os.path.join(out_dir, "latest.log"))

    result, reason = classify(timed_out, stopped_on_pass, returncode, capture, bool(expects))
    summary = {
        "result": result,
        "reason": reason,
        "label": label,
        "stamp": stamp,
        "runner": cmd[0],
        "elf": cmd[1],
        "args": list(extra_args),
        "seconds": seconds,
        "elapsed": round(elapsed, 3),
        "timed_out": timed_out,
        "stopped_on_pass": stopped_on_pass,
        "exit_code": None if (timed_out or stopped_on_pass) else returncode,
        "signal": _signal_name(returncode) if (returncode or 0) < 0 and not (timed_out or stopped_on_pass) else None,
        "expects": [{"pattern": p, "seen": capture.seen[p] is not None,
                     "line": (capture.seen[p] or {}).get("line"),
                     "text": (capture.seen[p] or {}).get("text")} for p, _ in capture.expects],
        "first_fatal": capture.first_fatal,
        "first_issue": capture.first_issue,
        "counts": capture.counts,
        "input": os.path.abspath(input_script) if input_script else None,
        "replay": capture.replay_loaded,
        "replay_problem": capture.replay_problem,
        "record": os.path.abspath(record) if record else None,
        "log": os.path.abspath(log_path),
        "lines": capture.total,
        "omitted": omitted,
        "silent_for": round(time.monotonic() - capture.last_output, 3) if timed_out else None,
    }
    notes = []
    if input_script and not capture.replay_loaded:
        notes.append("--input was given but the pad never said '[pad-replay] loaded': the runtime's "
                     "ps2_pad.cpp was not generated with padmap.py gen --replay, or the game never read the pad")
    if capture.replay_problem:
        notes.append(capture.replay_problem)
    summary["notes"] = notes
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    summary["_tail"] = lines[-200:]
    return summary


def _tail_text(lines, chars):
    text = "\n".join(lines)
    if len(text) > chars:
        text = text[-chars:]
        text = text[text.find("\n") + 1:] if "\n" in text else text
    return text


def print_summary(s, tail_chars):
    print("%s  %s" % (s["result"], s["reason"]))
    print("  runner  %s %s" % (s["runner"], s["elf"]))
    exit_text = ("killed by run.py after %.1f s" % s["elapsed"]) if (s["timed_out"] or s["stopped_on_pass"]) \
        else ("exit code %s after %.1f s" % (s["signal"] or s["exit_code"], s["elapsed"]))
    print("  run     %s" % exit_text)
    for e in s["expects"]:
        print("  expect  %-4s %s%s" % ("yes" if e["seen"] else "NO", e["pattern"],
                                      "  (line %d)" % e["line"] if e["seen"] else ""))
    c = s["counts"]
    print("  issues  stubs %d, syscalls %d, missing functions %d, IOP %d"
          % (c["stub"], c["syscall"], c["missing"] + c["missing_target"], c["iop"]))
    if s["first_fatal"]:
        print("  fatal   line %d: %s" % (s["first_fatal"]["line"], s["first_fatal"]["text"]))
    if s["input"]:
        print("  input   %s (%s)" % (s["input"], s["replay"] or "replay not confirmed"))
    for note in s["notes"]:
        print("  note    " + note)
    print("  log     %s (%d lines%s)" % (s["log"], s["lines"],
                                        ", %d omitted" % s["omitted"] if s["omitted"] else ""))
    if tail_chars > 0 and s.get("_tail"):
        print("  ---- last %d characters ----" % tail_chars)
        print(_tail_text(s["_tail"], tail_chars))


def _validate(args):
    if not os.path.isfile(args.runner):
        raise UsageError("runner not found: %s (build it first; see /console-anywhere plan)" % args.runner)
    if os.name != "nt" and not os.access(args.runner, os.X_OK):
        raise UsageError("runner is not executable: %s" % args.runner)
    if not os.path.isfile(args.elf):
        raise UsageError("ELF not found: %s (the game's main ELF in your extracted disc folder)" % args.elf)
    if not args.seconds > 0:
        raise UsageError("--seconds must be above 0")
    for pattern in args.expect:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise UsageError("--expect %r is not a valid regex: %s" % (pattern, exc))
    if args.input and not os.path.isfile(args.input):
        raise UsageError("input script not found: %s" % args.input)
    if args.input and args.record:
        raise UsageError("use --input or --record, not both")
    if args.keep < 10:
        raise UsageError("--keep must be at least 10")


def _run_from_args(args, label=None):
    return run_once(args.runner, args.elf, seconds=args.seconds, expects=args.expect,
                    input_script=args.input, record=args.record, out_dir=args.out,
                    extra_args=args.arg, cwd=args.cwd, keep=args.keep,
                    until_expect=args.until_expect, label=label)


def cmd_boot(args):
    _validate(args)
    s = _run_from_args(args)
    if args.json:
        s = dict(s)
        s.pop("_tail", None)
        print(json.dumps(s, indent=2))
    else:
        print_summary(s, args.tail_chars)
    return 0 if s["result"] == "PASS" else 1


def cmd_soak(args):
    _validate(args)
    if args.runs < 1:
        raise UsageError("--runs must be at least 1")
    runs = []
    for i in range(args.runs):
        s = _run_from_args(args, label="soak %d/%d" % (i + 1, args.runs))
        s.pop("_tail", None)
        runs.append(s)
        if not args.json:
            print("run %3d/%d  %-5s %5.1f s  %s" % (i + 1, args.runs, s["result"], s["elapsed"],
                                                   s["reason"][:100]))
        if args.stop_on_crash and s["result"] == "CRASH":
            break
    totals = {r: sum(1 for s in runs if s["result"] == r) for r in RESULTS}
    fatals = collections.Counter(s["first_fatal"]["text"] for s in runs if s["first_fatal"])
    summary = {
        "runs": len(runs), "requested": args.runs, "totals": totals,
        "flaky": len({s["result"] for s in runs}) > 1,
        "elapsed_min": min(s["elapsed"] for s in runs), "elapsed_max": max(s["elapsed"] for s in runs),
        "fatal_lines": [{"text": t, "count": n} for t, n in fatals.most_common()],
        "results": [{"stamp": s["stamp"], "result": s["result"], "reason": s["reason"],
                     "elapsed": s["elapsed"], "log": s["log"]} for s in runs],
    }
    path = os.path.join(args.out, "soak-%s.json" % utc_stamp())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    summary["file"] = os.path.abspath(path)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("")
        print("soak: %d run(s): %s" % (len(runs), ", ".join("%s %d" % (r, totals[r]) for r in RESULTS)))
        if summary["flaky"]:
            print("  FLAKY: the same boot gave different results. Timing-dependent bugs (threads, "
                  "vsync, IOP) show up like this; triage the odd run's log first.")
        for f in summary["fatal_lines"]:
            print("  x%d  %s" % (f["count"], f["text"][:140]))
        print("  summary %s" % summary["file"])
        print("  Same boot, same input, many times: it finds crashes that come and go. It does not "
              "play the game for you.")
    return 0 if totals["PASS"] == len(runs) == args.runs else 1


def cmd_last(args):
    path = os.path.join(args.out, "latest.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            s = json.load(fh)
    except FileNotFoundError:
        raise UsageError("no %s yet: run boot first" % path)
    except (OSError, ValueError) as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print_summary(s, 0)
    return 0 if s.get("result") == "PASS" else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Run a PS2Recomp port with a hard timeout, capture the log, judge the run.",
        epilog=("Results: PASS (every --expect seen, or no crash for the whole timeout), HANG "
                "(timeout before the expected lines), CRASH (fatal line, non-zero exit or signal), "
                "FAIL (exited by itself without the expected lines). Exit 0 only on PASS."))
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def common(sp):
        sp.add_argument("--runner", required=True, help="ps2EntryRunner (from your build folder)")
        sp.add_argument("--elf", required=True, help="the game's main ELF in your extracted disc folder")
        sp.add_argument("--seconds", type=float, default=15.0, help="hard timeout (default 15)")
        sp.add_argument("--expect", action="append", default=[], metavar="REGEX",
                        help="a log line that must appear (repeatable)")
        sp.add_argument("--until-expect", action="store_true",
                        help="stop as soon as every --expect has appeared")
        sp.add_argument("--input", help="replay script for the pad (sets PS2X_INPUT_SCRIPT)")
        sp.add_argument("--record", help="record live pad input to this file (sets PS2X_INPUT_RECORD)")
        sp.add_argument("--out", default=os.path.join("console", "runs"), help="log folder (default console/runs)")
        sp.add_argument("--arg", action="append", default=[], help="extra argument for the runner")
        sp.add_argument("--cwd", help="working folder for the runner")
        sp.add_argument("--keep", type=int, default=2000, help="lines kept at each end of a huge log")
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("boot", help="one run with a hard timeout")
    common(sp)
    sp.add_argument("--tail-chars", type=int, default=5000, help="characters of the log tail to print")
    sp.set_defaults(func=cmd_boot)

    sp = sub.add_parser("soak", help="the same boot N times, summed up")
    common(sp)
    sp.add_argument("--runs", type=int, default=10)
    sp.add_argument("--stop-on-crash", action="store_true")
    sp.set_defaults(func=cmd_soak)

    sp = sub.add_parser("last", help="print console/runs/latest.json")
    sp.add_argument("--out", default=os.path.join("console", "runs"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_last)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
