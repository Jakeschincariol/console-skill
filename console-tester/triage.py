#!/usr/bin/env python3
"""triage.py: read PS2Recomp runtime logs and rank what to fix next (console-tester).

Parses the runtime's own stderr lines (unimplemented stubs and syscalls, missing
recompiled functions, unhandled IOP RPCs, disc and file errors, fatal errors),
clusters them by PC, names the function each PC is in from a Ghidra CSV map
(Name,Start,End,Size with End exclusive, from ExportPS2Functions.java), ranks them
and says which /console-* skill owns each one. `compare` diffs two runs.

Python 3.8+ standard library only.
"""

import argparse
import bisect
import csv
import json
import os
import re
import sys

HEX = r"[0-9a-fA-F]+"

RX = {
    "stub": re.compile(r"Warning: Unimplemented PS2 stub called\. name=(?P<name>\S+) PC=0x(?P<pc>%s), "
                       r"RA=0x(?P<ra>%s)" % (HEX, HEX)),
    "stub_suppressed": re.compile(r"Warning: Further calls to PS2 stub '(?P<name>[^']+)' are suppressed "
                                  r"after (?P<n>\d+) warnings"),
    "syscall_todo": re.compile(r"\[Syscall TODO\] encoded=0x(?P<enc>%s) v1=0x(?P<v1>%s) v0=0x(?P<v0>%s)"
                               r".*? pc=0x(?P<pc>%s)" % (HEX, HEX, HEX, HEX)),
    "syscall": re.compile(r"Warning: Unimplemented PS2 syscall called\. PC=0x(?P<pc>%s), RA=0x(?P<ra>%s), "
                          r"Encoded=0x(?P<enc>%s), v0=0x(?P<v0>%s), v1=0x(?P<v1>%s), Chosen=0x(?P<id>%s)"
                          % (HEX, HEX, HEX, HEX, HEX, HEX)),
    "syscall_hits": re.compile(r"-> Unknown syscallId=0x(?P<id>%s) hits=(?P<hits>\d+)" % HEX),
    "missing": re.compile(r"Error: No exact recompiled function for guest PC 0x(?P<pc>%s)"
                          r"(?:.*? codeRegion=(?P<code>yes|no))?" % HEX),
    "branch": re.compile(r"\[guest-branch:missing-target\] kind=(?P<kind>\S+) op=(?P<op>\S+) "
                         r"source=0x(?P<src>%s) target=0x(?P<target>%s) pc=0x(?P<pc>%s) ra=0x(?P<ra>%s)"
                         r"(?:.*? codeRegion=(?P<code>yes|no))?" % (HEX, HEX, HEX, HEX)),
    "iop": re.compile(r"\[IOP/RPC trace:unhandled\] sid=0x(?P<sid>%s) rpc=0x(?P<rpc>%s) pc=0x(?P<pc>%s) "
                      r"ra=0x(?P<ra>%s)(?:.*? loadedModules=\[(?P<mods>[^\]]*)\])?" % (HEX, HEX, HEX, HEX)),
    "cdread": re.compile(r"\[sceCdRead\] unresolved request pc=0x(?P<pc>%s) ra=0x(?P<ra>%s)" % (HEX, HEX)),
    "elfpart": re.compile(r"\[SifLoadElfPart\] failed path=\"(?P<path>[^\"]*)\"(?:.*? reason=(?P<reason>.*))?"),
    "fio": re.compile(r"^\s*(?:ps2_stub )?(?P<fn>f\w+) error: (?P<msg>.*)$"),
    "override": re.compile(r"\[game_overrides\] unresolved handler '(?P<name>[^']+)' for address 0x(?P<pc>%s)"
                           % HEX),
    "table": re.compile(r"\[function-table\] cannot replace guest PC 0x(?P<pc>%s)" % HEX),
    "mpeg_off": re.compile(r"\[MPEG\] runtime built without FFmpeg"),
    "mpeg_err": re.compile(r"\[MPEG\] (?P<msg>(?:FFmpeg|failed|decoder|parser).*)$"),
    "replay_bad": re.compile(r"\[pad-replay\] (?P<msg>.*replay disabled.*|cannot open .*)$"),
    "fatal_exec": re.compile(r"Error during program execution: (?P<msg>.*)$"),
    "fatal_main": re.compile(r"\[main\] fatal exception: (?P<msg>.*)$"),
    "fatal_term": re.compile(r"\[terminate\] (?P<msg>.*)$"),
    "fatal_init": re.compile(r"(?P<msg>Failed to initialize PS2 runtime.*|Failed to initialize PS2 memory.*|"
                             r"Failed to bind runtime core subsystems.*)$"),
    "fatal_elf": re.compile(r"(?P<msg>Failed to load ELF file: .*|Failed to open ELF file: .*|"
                            r"Invalid ELF magic number|Not a MIPS executable ELF file|"
                            r"Unsupported ELF format.*|ELF file is too small: .*)$"),
}
STUB_THROW = re.compile(r"Unimplemented PS2 stub called: (?P<name>\S+)")

# Stub name prefix -> (owning skill, likely file under ps2xRuntime/src/lib/Kernel/Stubs/).
STUB_OWNERS = [
    ("scePad", "/console-controller", "Pad.cpp"),
    ("sceMc", "/console-saves", "MemoryCard.cpp"),
    ("mc", "/console-saves", "MemoryCard.cpp"),
    ("sceSd", "/console-sound", "Audio.cpp"),
    ("sceSynth", "/console-sound", "Audio.cpp"),
    ("sceSS", "/console-sound", "Audio.cpp"),
    ("sceGs", "/console-graphics", "GS.cpp"),
    ("sceGif", "/console-graphics", "GS.cpp"),
    ("sceVif", "/console-graphics", "GS.cpp"),
    ("sceVu", "/console-graphics", "VU.cpp"),
    ("sceVpu", "/console-graphics", "VU.cpp"),
    ("sceIpu", "/console-graphics", "IPU.cpp"),
    ("sceMpeg", "/console-graphics", "MPEG.cpp"),
    ("sceeFont", "/console-graphics", "Font.cpp"),
    ("sceCd", "/console-backend", "CD.cpp"),
    ("sceDma", "/console-backend", "DMA.cpp"),
    ("sceDeci", "/console-backend", "Deci2.cpp"),
    ("sceFs", "/console-backend", "FileIO.cpp"),
    ("sceOpen", "/console-backend", "FileIO.cpp"),
    ("sceRead", "/console-backend", "FileIO.cpp"),
    ("sceWrite", "/console-backend", "FileIO.cpp"),
    ("sceClose", "/console-backend", "FileIO.cpp"),
    ("sceLseek", "/console-backend", "FileIO.cpp"),
    ("sceSif", "/console-backend", "SIF.cpp"),
    ("isceSif", "/console-backend", "SIF.cpp"),
    ("sceRpc", "/console-backend", "RPC.cpp"),
    ("sceTty", "/console-backend", "TTY.cpp"),
    ("scePrintf", "/console-backend", "TTY.cpp"),
]

TIERS = {"fatal": 0, "missing": 1, "stub": 2, "syscall": 3, "iop": 4, "io": 5, "mod": 6, "build": 7,
         "input": 8}
TIER_NOTE = {
    0: "stopped the run", 1: "jump into code the recompiler never emitted",
    2: "unimplemented stub (throws: stops the game thread)", 3: "unimplemented syscall (returns 0)",
    4: "IOP request nobody answered", 5: "disc or file I/O problem", 6: "override problem",
    7: "build option", 8: "test input",
}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- Ghidra map

class FunctionMap:
    """Ghidra CSV map (Name,Start,End,Size; End exclusive). Labels may overlap functions."""

    def __init__(self, records):
        self.records = sorted(records, key=lambda r: (r[0], r[1]))
        self.starts = [r[0] for r in self.records]

    @classmethod
    def load(cls, path):
        records = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                if not header or [h.strip().lower() for h in header[:3]] != ["name", "start", "end"]:
                    raise UsageError("%s is not a Ghidra function map (expected the header Name,Start,End,Size)"
                                     % path)
                for number, row in enumerate(reader, 2):
                    if not row or not "".join(row).strip():
                        continue
                    if len(row) < 3:
                        raise UsageError("%s line %d: expected Name,Start,End,Size" % (path, number))
                    try:
                        start = int(row[1].strip(), 16)
                        end = int(row[2].strip(), 16)
                    except ValueError:
                        raise UsageError("%s line %d: Start and End must be hex like 0x00100230"
                                         % (path, number))
                    if end <= start:
                        end = start + 4
                    records.append((start, end, row[0].strip()))
        except FileNotFoundError:
            raise UsageError("no such file: %s" % path)
        except OSError as exc:
            raise UsageError("cannot read %s: %s" % (path, exc))
        return cls(records)

    def lookup(self, addr):
        """Return (name, offset) for the narrowest record containing addr, or None."""
        if addr is None or not self.records:
            return None
        i = bisect.bisect_right(self.starts, addr) - 1
        best = None
        steps = 0
        while i >= 0 and steps < 256:
            start, end, name = self.records[i]
            if start <= addr < end and (best is None or (end - start) < (best[1] - best[0])):
                best = (start, end, name)
            i -= 1
            steps += 1
        if best is None:
            return None
        return best[2], addr - best[0]

    def describe(self, addr):
        hit = self.lookup(addr)
        if hit is None:
            return None
        name, off = hit
        return name if off == 0 else "%s+0x%x" % (name, off)


# ---------------------------------------------------------------- parsing

def _h(text):
    return int(text, 16)


def stub_owner(name):
    for prefix, owner, filename in STUB_OWNERS:
        if name.startswith(prefix):
            return owner, filename
    return "/console-backend", None


class Issue:
    def __init__(self, key, kind, title, owner, origin, line):
        self.key = key
        self.kind = kind
        self.title = title
        self.owner = owner
        self.count = 0
        self.min_count = 0
        self.first = origin
        self.first_text = line.strip()[:300]
        self.pc = None
        self.ra = None
        self.fatal = False
        self.details = {}
        self.next = ""

    @property
    def tier(self):
        return 0 if self.fatal else TIERS[self.kind]

    def to_dict(self, fmap=None):
        d = {
            "key": self.key, "kind": self.kind, "title": self.title, "owner": self.owner,
            "count": max(self.count, self.min_count), "fatal": self.fatal, "tier": self.tier,
            "first_log": self.first[0], "first_line": self.first[1], "first_text": self.first_text,
            "pc": "0x%08x" % self.pc if self.pc is not None else None,
            "ra": "0x%08x" % self.ra if self.ra is not None else None,
            "next": self.next, "details": self.details,
        }
        if fmap is not None:
            d["function"] = fmap.describe(self.pc)
            d["caller"] = fmap.describe(self.ra)
        return d


class Triage:
    def __init__(self):
        self.issues = {}
        self.order = 0
        self.lines = 0
        self.logs = []

    def _get(self, key, kind, title, owner, origin, line):
        issue = self.issues.get(key)
        if issue is None:
            issue = Issue(key, kind, title, owner, origin, line)
            issue.seq = self.order
            self.order += 1
            self.issues[key] = issue
        return issue

    def feed(self, line, origin):
        self.lines += 1
        m = RX["stub"].search(line)
        if m:
            name = m.group("name")
            owner, filename = stub_owner(name)
            it = self._get("stub:" + name, "stub", "unimplemented stub %s" % name, owner, origin, line)
            it.count += 1
            if it.pc is None:
                it.pc, it.ra = _h(m.group("pc")), _h(m.group("ra"))
            if filename:
                it.details["likely_file"] = "ps2xRuntime/src/lib/Kernel/Stubs/" + filename
            it.next = ("implement %s in the runtime (likely Kernel/Stubs/%s) or bind it by address; "
                       "ret0/ret1/reta0 only to triage" % (name, filename or "<the matching file>.cpp"))
            return
        m = RX["stub_suppressed"].search(line)
        if m:
            name = m.group("name")
            owner, _ = stub_owner(name)
            it = self._get("stub:" + name, "stub", "unimplemented stub %s" % name, owner, origin, line)
            it.min_count = max(it.min_count, int(m.group("n")) + 1)
            it.details["suppressed"] = True
            return
        m = RX["syscall"].search(line)
        if m:
            sid = _h(m.group("id"))
            it = self._get("syscall:0x%x" % sid, "syscall", "unimplemented syscall 0x%x" % sid,
                           "/console-backend", origin, line)
            it.count += 1
            if it.pc is None:
                it.pc, it.ra = _h(m.group("pc")), _h(m.group("ra"))
            it.details["encoded"] = "0x%x" % _h(m.group("enc"))
            it.next = ("name it (/console-backend stubs.py syscall 0x%x) and implement it in "
                       "Kernel/Syscalls/ (Dispatcher.cpp routes the number)" % sid)
            return
        m = RX["syscall_hits"].search(line)
        if m:
            sid = _h(m.group("id"))
            it = self.issues.get("syscall:0x%x" % sid)
            if it is not None:
                it.min_count = max(it.min_count, int(m.group("hits")))
            return
        if RX["syscall_todo"].search(line):
            return  # the "Warning: Unimplemented PS2 syscall" line that follows carries the same event
        m = RX["branch"].search(line)
        if m:
            target = _h(m.group("target"))
            it = self._missing(target, origin, line, m.group("code"))
            if it.count == 0:
                it.count = 1  # the lookup line, when there is one, comes first and is counted
            it.ra = _h(m.group("ra"))
            it.details["branch"] = m.group("kind")
            it.details["source"] = "0x%08x" % _h(m.group("src"))
            return
        m = RX["missing"].search(line)
        if m:
            it = self._missing(_h(m.group("pc")), origin, line, m.group("code"))
            it.count += 1
            return
        m = RX["iop"].search(line)
        if m:
            sid, rpc = _h(m.group("sid")), _h(m.group("rpc"))
            it = self._get("iop:0x%x:0x%x" % (sid, rpc), "iop", "IOP RPC sid 0x%x rpc 0x%x unhandled"
                           % (sid, rpc), "/console-backend", origin, line)
            it.count += 1
            if it.pc is None:
                it.pc, it.ra = _h(m.group("pc")), _h(m.group("ra"))
            if m.group("mods"):
                it.details["loaded_modules"] = m.group("mods")
            it.next = ("find which IRX serves sid 0x%x (loadedModules) and answer it: run the real "
                       "module in ps2xIOP or add an HLE handler" % sid)
            return
        m = RX["cdread"].search(line)
        if m:
            pc = _h(m.group("pc"))
            it = self._get("io:sceCdRead:0x%x" % pc, "io", "sceCdRead could not resolve a disc read",
                           "/console-backend", origin, line)
            it.count += 1
            it.pc, it.ra = pc, _h(m.group("ra"))
            it.next = ("the read points outside the extracted files: check cdRoot (the ELF's folder) "
                       "holds the whole disc, or give the runtime the disc image (cdImage)")
            return
        m = RX["elfpart"].search(line)
        if m:
            path = m.group("path")
            it = self._get("io:SifLoadElfPart:" + path, "io", "module or overlay %s did not load" % path,
                           "/console-backend", origin, line)
            it.count += 1
            if m.group("reason"):
                it.details["reason"] = m.group("reason").strip()
            it.next = ("is %s in the extracted disc folder (same case)? If it is, the loader needs work"
                       % path)
            return
        m = RX["override"].search(line)
        if m:
            name = m.group("name")
            it = self._get("mod:handler:" + name, "mod", "override binds unknown handler '%s'" % name,
                           "/console-mod", origin, line)
            it.count += 1
            it.pc = _h(m.group("pc"))
            it.next = "use a handler name from ps2_call_list.h, or replaceFunction with your own function"
            return
        m = RX["table"].search(line)
        if m:
            pc = _h(m.group("pc"))
            it = self._get("mod:table:0x%x" % pc, "mod", "override address outside the function table",
                           "/console-mod", origin, line)
            it.count += 1
            it.pc = pc
            it.next = "the address is not a recompiled function start in this ELF build: check it in Ghidra"
            return
        m = RX["fio"].search(line)
        if m:
            msg = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b", "N", m.group("msg").strip())
            it = self._get("io:%s:%s" % (m.group("fn"), msg), "io", "%s error: %s" % (m.group("fn"), msg),
                           "/console-backend", origin, line)
            it.count += 1
            it.next = "a guest file call failed: check the path translation and the extracted files"
            return
        if RX["mpeg_off"].search(line):
            it = self._get("build:ffmpeg", "build", "runtime built without FFmpeg: FMV shows stub frames",
                           "/console-anywhere", origin, line)
            it.count += 1
            it.next = "rebuild with FFmpeg available (PS2X_ENABLE_FFMPEG=ON and the libraries installed)"
            return
        m = RX["mpeg_err"].search(line)
        if m:
            msg = re.sub(r"0x[0-9a-fA-F]+|-?\b\d+\b", "N", m.group("msg").strip())
            it = self._get("io:mpeg:" + msg, "io", "MPEG video: " + msg, "/console-graphics", origin, line)
            it.count += 1
            it.next = "cutscene decode problem: check the FFmpeg build first (/console-anywhere doctor)"
            return
        m = RX["replay_bad"].search(line)
        if m:
            it = self._get("input:replay", "input", "input replay script rejected", "/console-controller",
                           origin, line)
            it.count += 1
            it.next = "fix the script (padmap.py check --script FILE)"
            return
        for kind in ("fatal_exec", "fatal_main", "fatal_term", "fatal_init", "fatal_elf"):
            m = RX[kind].search(line)
            if m:
                self._fatal(kind, m.group("msg").strip(), origin, line)
                return

    def _missing(self, pc, origin, line, code):
        it = self._get("missing:0x%x" % pc, "missing", "no recompiled function at 0x%08x" % pc,
                       "/console-reverse", origin, line)
        if it.pc is None:
            it.pc = pc
        if code:
            it.details["code_region"] = code
        if it.details.get("code_region") == "no":
            it.next = ("0x%08x is not code: something jumped through a bad pointer. Look at the caller "
                       "(ra) and A/B it against PCSX2 (abdiff.py)" % pc)
        else:
            it.next = ("add 0x%08x to entry_points in the TOML (or define a function there in Ghidra and "
                       "re-export), then re-run ps2_recomp and rebuild" % pc)
        return it

    def _fatal(self, kind, msg, origin, line):
        m = STUB_THROW.search(msg)
        if m:
            name = m.group("name")
            owner, _ = stub_owner(name)
            it = self._get("stub:" + name, "stub", "unimplemented stub %s" % name, owner, origin, line)
            it.fatal = True
            it.count = max(it.count, 1)
            if not it.next:
                it.next = "implement %s in the runtime or bind it by address" % name
            return
        norm = re.sub(r"0x[0-9a-fA-F]+", "0x?", msg)
        if kind == "fatal_term":
            # "[terminate] unhandled exception" and its detail line are one event.
            it = self._get("fatal:terminate", "fatal", "unhandled C++ exception ([terminate])",
                           "/console-backend", origin, line)
            it.fatal = True
            if msg.startswith("unhandled"):
                it.count += 1
            else:
                it.count = max(it.count, 1)
                it.details["exception"] = msg[:200]
            it.next = "read the exception text and the lines before it: the cause comes first"
            return
        if kind == "fatal_elf":
            owner, nxt = "/console-reverse", "point the runner at the game's main ELF (SYSTEM.CNF BOOT2)"
        elif kind == "fatal_init":
            owner, nxt = "/console-anywhere", "the host side failed to start (window, GL, memory): check the build"
        else:
            owner, nxt = "/console-backend", "read the lines just before it in the log: the cause comes first"
        it = self._get("fatal:" + norm, "fatal", msg[:160] or kind, owner, origin, line)
        it.fatal = True
        it.count += 1
        it.next = nxt

    def scan_file(self, path, label=None):
        label = label or path
        index = len(self.logs)
        self.logs.append(label)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for number, line in enumerate(fh, 1):
                    self.feed(line, (label, number, index))
        except FileNotFoundError:
            raise UsageError("no such file: %s" % path)
        except IsADirectoryError:
            raise UsageError("%s is a folder, not a log file" % path)
        except OSError as exc:
            raise UsageError("cannot read %s: %s" % (path, exc))

    def ranked(self):
        def sort_key(it):
            return (it.tier, it.first[2], it.first[1], -max(it.count, it.min_count))
        return sorted(self.issues.values(), key=sort_key)


def scan_logs(paths):
    t = Triage()
    for p in paths:
        t.scan_file(p)
    return t


# ---------------------------------------------------------------- output

def _where(d):
    parts = []
    if d.get("pc"):
        parts.append("pc %s%s" % (d["pc"], " (%s)" % d["function"] if d.get("function") else ""))
    if d.get("ra"):
        parts.append("ra %s%s" % (d["ra"], " (%s)" % d["caller"] if d.get("caller") else ""))
    return ", ".join(parts) or "-"


def render_text(dicts, logs, lines):
    out = ["triage: %d log(s), %d lines, %d distinct issue(s)" % (len(logs), lines, len(dicts))]
    if not dicts:
        out.append("  nothing the runtime flagged. If the game still misbehaves, compare against PCSX2 (abdiff.py).")
        return "\n".join(out)
    for rank, d in enumerate(dicts, 1):
        flag = " [FATAL]" if d["fatal"] else ""
        out.append("")
        out.append("%2d. %s%s  x%d  -> %s" % (rank, d["title"], flag, d["count"], d["owner"]))
        out.append("    where: %s" % _where(d))
        out.append("    first: %s line %d: %s" % (os.path.basename(str(d["first_log"])), d["first_line"],
                                                  d["first_text"][:140]))
        if d["next"]:
            out.append("    next:  %s" % d["next"])
    return "\n".join(out)


def _md(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(dicts):
    rows = ["| # | issue | where | count | first seen | owner | next step |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for rank, d in enumerate(dicts, 1):
        rows.append("| %d | %s%s | %s | %d | %s:%d | %s | %s |" % (
            rank, _md(d["title"]), " (fatal)" if d["fatal"] else "", _md(_where(d)), d["count"],
            _md(os.path.basename(str(d["first_log"]))), d["first_line"], d["owner"], _md(d["next"])))
    return "\n".join(rows)


def cmd_scan(args):
    fmap = FunctionMap.load(args.map) if args.map else None
    t = scan_logs(args.logs)
    dicts = [it.to_dict(fmap) for it in t.ranked()]
    if args.top:
        dicts = dicts[:args.top]
    if args.json:
        print(json.dumps({"logs": t.logs, "lines": t.lines, "issues": dicts}, indent=2))
    elif args.markdown:
        print(render_markdown(dicts))
    else:
        print(render_text(dicts, t.logs, t.lines))
    return 1 if t.issues else 0


def compare(old_paths, new_paths, fmap=None):
    old = scan_logs(old_paths)
    new = scan_logs(new_paths)
    fixed, still, added = [], [], []
    for it in old.ranked():
        if it.key in new.issues:
            n = new.issues[it.key]
            d = n.to_dict(fmap)
            d["old_count"] = max(it.count, it.min_count)
            still.append(d)
        else:
            fixed.append(it.to_dict(fmap))
    for it in new.ranked():
        if it.key not in old.issues:
            added.append(it.to_dict(fmap))
    return {"fixed": fixed, "still": still, "new": added}


def cmd_compare(args):
    fmap = FunctionMap.load(args.map) if args.map else None
    result = compare([args.old], [args.new], fmap)
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.markdown:
        for label in ("new", "still", "fixed"):
            print("### %s (%d)\n" % (label, len(result[label])))
            if result[label]:
                print(render_markdown(result[label]))
            print("")
    else:
        print("compare: %s -> %s" % (args.old, args.new))
        print("  fixed %d, still there %d, new %d" % (len(result["fixed"]), len(result["still"]),
                                                     len(result["new"])))
        for d in result["fixed"]:
            print("  FIXED  %s" % d["title"])
        for d in result["still"]:
            print("  STILL  %s  (x%d -> x%d)" % (d["title"], d["old_count"], d["count"]))
        for d in result["new"]:
            print("  NEW    %s%s  -> %s  [%s]" % (d["title"], " [FATAL]" if d["fatal"] else "", d["owner"],
                                                 _where(d)))
        if result["new"]:
            print("  New issues appeared: the change may have broken something, or the game got further "
                  "and met the next blocker. Read the first NEW line.")
    return 1 if result["new"] else 0


def cmd_lookup(args):
    fmap = FunctionMap.load(args.map)
    rows = []
    for text in args.addrs:
        try:
            addr = int(text, 16)
        except ValueError:
            raise UsageError("%r is not a hex address" % text)
        rows.append({"addr": "0x%08x" % addr, "function": fmap.describe(addr)})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print("%s  %s" % (r["addr"], r["function"] or "(not in the map)"))
    return 0 if all(r["function"] for r in rows) else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="triage.py",
        description="Rank what to fix next from PS2Recomp runtime logs, and diff two runs.",
        epilog="Typical: triage.py scan console/runs/latest.log --map console/functions.csv --markdown")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sp = sub.add_parser("scan", help="parse one or more logs, cluster by PC, rank, name the owner")
    sp.add_argument("logs", nargs="+", help="runtime log files (run.py writes console/runs/*.log)")
    sp.add_argument("--map", help="Ghidra CSV map (Name,Start,End,Size) to name each PC")
    sp.add_argument("--top", type=int, default=0, help="show only the first N issues")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--markdown", action="store_true", help="a table to paste into console/STATE.md")
    g.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("compare", help="what got fixed, what is still there, what is new")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.add_argument("--map")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--markdown", action="store_true")
    g.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("lookup", help="name the function each address is in (Ghidra CSV map)")
    sp.add_argument("addrs", nargs="+", help="hex addresses, like 0x00123456")
    sp.add_argument("--map", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_lookup)
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


if __name__ == "__main__":
    sys.exit(main())
