#!/usr/bin/env python3
"""Turn PS2Recomp runtime logs into a ranked list of backend fixes.

Subcommands:
  scan      read one or more runtime logs: unimplemented stubs, syscall TODOs, missing functions,
            IOP/RPC gaps, untranslated instructions, override errors; count, first PC/RA, and the
            fix tool for each (TOML binding, runtime C++ file, entry point + recompile, or override)
  handlers  list the runtime's real handler names (PS2_SYSCALL_LIST, PS2_STUB_LIST), the syscall
            numbers Dispatcher.cpp maps, and the file each handler lives in (needs --repo)
  syscall   name an EE kernel syscall number (built-in table, plus the checkout's dispatcher)

The log lines matched are the exact text the runtime prints (PS2Recomp, Oct 2026). Logs are
streamed line by line. With --repo PATH (a PS2Recomp checkout) answers use the real handler lists;
without it they fall back to a built-in table and name-based guesses, and say so.
"""

import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------- the EE kernel syscalls we are sure of
# Standard PS2 EE kernel (BIOS) syscall numbers, cross-checked against the PS2Recomp dispatcher.
# Negative numbers are the interrupt-context ("i") variants. Not exhaustive: the checkout's
# Kernel/Syscalls/Dispatcher.cpp (--repo) is the authority for what the runtime handles.
BUILTIN_SYSCALLS = {
    0x01: "ResetEE", 0x02: "SetGsCrt", 0x04: "Exit", 0x06: "LoadExecPS2", 0x07: "ExecPS2",
    0x10: "AddIntcHandler", 0x11: "RemoveIntcHandler", 0x12: "AddDmacHandler", 0x13: "RemoveDmacHandler",
    0x14: "EnableIntc", 0x15: "DisableIntc", 0x16: "EnableDmac", 0x17: "DisableDmac",
    0x18: "SetAlarm", 0x19: "ReleaseAlarm",
    -0x1A: "iEnableIntc", -0x1B: "iDisableIntc", -0x1C: "iEnableDmac", -0x1D: "iDisableDmac",
    -0x1E: "iSetAlarm", -0x1F: "iReleaseAlarm",
    0x20: "CreateThread", 0x21: "DeleteThread", 0x22: "StartThread", 0x23: "ExitThread",
    0x24: "ExitDeleteThread", 0x25: "TerminateThread", -0x26: "iTerminateThread",
    0x27: "DisableDispatchThread", 0x28: "EnableDispatchThread",
    0x29: "ChangeThreadPriority", -0x2A: "iChangeThreadPriority",
    0x2B: "RotateThreadReadyQueue", -0x2C: "iRotateThreadReadyQueue",
    0x2D: "ReleaseWaitThread", -0x2E: "iReleaseWaitThread", 0x2F: "GetThreadId",
    0x30: "ReferThreadStatus", -0x31: "iReferThreadStatus", 0x32: "SleepThread", 0x33: "WakeupThread",
    -0x34: "iWakeupThread", 0x35: "CancelWakeupThread", -0x36: "iCancelWakeupThread",
    0x37: "SuspendThread", -0x38: "iSuspendThread", 0x39: "ResumeThread", -0x3A: "iResumeThread",
    0x3C: "SetupThread", 0x3D: "SetupHeap", 0x3E: "EndOfHeap",
    0x40: "CreateSema", 0x41: "DeleteSema", 0x42: "SignalSema", -0x43: "iSignalSema",
    0x44: "WaitSema", 0x45: "PollSema", -0x46: "iPollSema", 0x47: "ReferSemaStatus",
    -0x48: "iReferSemaStatus",
    0x4A: "SetOsdConfigParam", 0x4B: "GetOsdConfigParam",
    0x5C: "EnableIntcHandler", -0x5C: "iEnableIntcHandler", 0x5D: "DisableIntcHandler",
    -0x5D: "iDisableIntcHandler", 0x5E: "EnableDmacHandler", -0x5E: "iEnableDmacHandler",
    0x5F: "DisableDmacHandler", -0x5F: "iDisableDmacHandler",
    0x61: "EnableCache", 0x62: "DisableCache", 0x64: "FlushCache", -0x68: "iFlushCache",
    0x70: "GsGetIMR", -0x70: "iGsGetIMR", 0x71: "GsPutIMR", -0x71: "iGsPutIMR",
    0x73: "SetVSyncFlag", 0x74: "SetSyscall",
    0x76: "sceSifDmaStat", -0x76: "isceSifDmaStat", 0x77: "sceSifSetDma", -0x77: "isceSifSetDma",
    0x78: "sceSifSetDChain", -0x78: "isceSifSetDChain", 0x79: "sceSifSetReg", 0x7A: "sceSifGetReg",
    0x7B: "ExecOSD", 0x7C: "Deci2Call", 0x7F: "GetMemorySize",
}

RT = "ps2xRuntime/src/lib/"
CALL_LIST = "ps2xRuntime/include/ps2_call_list.h"
DISPATCHER = RT + "Kernel/Syscalls/Dispatcher.cpp"

# name-based guesses, used only without --repo
STUB_PREFIX_FILES = [
    (r"^sceCd", "Kernel/Stubs/CD.cpp"), (r"^(sceDma|DmaAddr)", "Kernel/Stubs/DMA.cpp"),
    (r"^sceDeci2", "Kernel/Stubs/Deci2.cpp"),
    (r"^(sceFs|sceOpen|sceClose|sceRead|sceWrite|sceLseek|sceIoctl|open|close|read|write|lseek|stat|fstat)$|^sceFs",
     "Kernel/Stubs/FileIO.cpp"),
    (r"^sceeFont", "Kernel/Stubs/Font.cpp"), (r"^(sceGs|sceGif|sceVif1)", "Kernel/Stubs/GS.cpp"),
    (r"^sceIpu", "Kernel/Stubs/IPU.cpp"), (r"^sceMpeg", "Kernel/Stubs/MPEG.cpp"),
    (r"^(sceMc|mc[A-Z])", "Kernel/Stubs/MemoryCard.cpp"), (r"^(scePad|Pad[A-Z])", "Kernel/Stubs/Pad.cpp"),
    (r"^sceRpc", "Kernel/Stubs/RPC.cpp"), (r"^i?sceSif", "Kernel/Stubs/SIF.cpp"),
    (r"^(sceTty|scePrintf|sceResettty)", "Kernel/Stubs/TTY.cpp"), (r"^(sceVu0|sceVpu0)", "Kernel/Stubs/VU.cpp"),
    (r"^(sceSd|sceSSyn|sceSynth)", "Kernel/Stubs/Audio.cpp"),
    (r"^(ret0|ret1|reta0|.*_r|__malloc_(un)?lock|malloc_extend_top)$", "Kernel/Stubs/Compatibility.cpp"),
    (r"^(_?_?)(malloc|free|calloc|realloc|memalign|mem[a-z]+|str[a-z]+|v?s?n?printf|v?fprintf|puts|"
     r"sin|cos|tan|atan2?|sqrt|pow|exp|log|log10|floor|ceil|fabs|abs|rand|srand|f(open|close|read|write|seek|tell|flush)|"
     r"exit|getpid|divdi3)$", "Kernel/Stubs/LibC.cpp"),
]
SYSCALL_NAME_FILES = [
    (r"Sema|EventFlag|Alarm", "Kernel/Syscalls/Sync.cpp"),
    (r"Intc|Dmac|VSync", "Kernel/Syscalls/Interrupt.cpp"),
    (r"Thread|Cache|^ResetEE$|^SetMemoryMode$|^Exit$", "Kernel/Syscalls/Thread.cpp"),
    (r"^i?sceSif", "Kernel/Stubs/SIF.cpp"),
    (r"^Sif", "Kernel/Syscalls/RPC.cpp"),
    (r"^fio", "Kernel/Syscalls/FileIO.cpp"),
    (r"^Deci2", "Kernel/Syscalls/Deci2.cpp"),
    (r"Gs|Osd|^Setup|EndOfHeap|MemorySize|SetSyscall|Exec|Copy|EntryAddress|TLB|FindAddress", "Kernel/Syscalls/System.cpp"),
]
AUTO_NAME_RE = re.compile(r"^(?:sub|FUN|func|entry|fn|LAB)_(?:0x)?([0-9A-Fa-f]{6,8})(?:_0x[0-9A-Fa-f]+)?$")


class InputError(Exception):
    """Bad input: exit 2 with a one-line message."""


# ---------------------------------------------------------------- numbers

def parse_int(text):
    t = text.strip().replace("_", "")
    neg = t.startswith("-")
    if neg:
        t = t[1:]
    try:
        v = int(t, 16) if t.lower().startswith("0x") else int(t, 10)
    except ValueError:
        raise InputError("%r is not a number (use 0x3C, 60, -0x1A or 0xFFFFFFE6)" % text)
    return -v if neg else v


def signed_syscall(v):
    """Normalise to the signed form the kernel uses: 0xFFFFFFE6 -> -0x1A."""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def fmt_syscall(n):
    if n < 0:
        return "-0x%X (0x%08X)" % (-n, n & 0xFFFFFFFF)
    return "0x%X" % n


# ---------------------------------------------------------------- the checkout

class Knowledge(object):
    """What we know about the runtime: from a PS2Recomp checkout, or built-in guesses."""

    def __init__(self, repo=None):
        self.repo = os.path.abspath(repo) if repo else None
        self.syscall_names = None
        self.stub_names = None
        self.dispatch = None          # signed number -> handler name
        self.defined = {}             # handler name -> relative file
        self.todo = {}                # handler name -> relative file where it is still TODO_NAMED
        if repo:
            self._load_repo()

    def _read(self, rel):
        path = os.path.join(self.repo, *rel.split("/"))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            raise InputError("%s is not a PS2Recomp checkout (missing %s)" % (self.repo, rel))

    def _load_repo(self):
        text = self._read(CALL_LIST)
        self.syscall_names = macro_names(text, "PS2_SYSCALL_LIST")
        self.stub_names = macro_names(text, "PS2_STUB_LIST")
        if not self.syscall_names and not self.stub_names:
            raise InputError("%s has no PS2_SYSCALL_LIST / PS2_STUB_LIST" % CALL_LIST)
        self.dispatch = parse_dispatcher(self._read(DISPATCHER))
        kernel = os.path.join(self.repo, *(RT + "Kernel").split("/"))
        for base, dirs, files in os.walk(kernel):
            dirs.sort()
            for f in sorted(files):
                if not f.endswith(".cpp"):
                    continue
                full = os.path.join(base, f)
                rel = os.path.relpath(full, os.path.join(self.repo, *RT.rstrip("/").split("/"))).replace(os.sep, "/")
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        src = fh.read()
                except OSError:
                    continue
                for m in DEF_RE.finditer(src):
                    self.defined.setdefault(m.group(1), rel)
                for m in TODO_RE.finditer(src):
                    self.todo.setdefault(m.group(1), rel)

    @property
    def has_repo(self):
        return self.repo is not None

    def resolve(self, name):
        """The runtime's own alias rule: exact, or with/without one leading underscore."""
        if not self.has_repo or not name:
            return None, None
        for kind, names in (("syscall", self.syscall_names), ("stub", self.stub_names)):
            if name in names:
                return kind, name
            if name.startswith("_") and name[1:] in names:
                return kind, name[1:]
            if "_" + name in names:
                return kind, "_" + name
        return None, None

    def file_for(self, name, syscall=False):
        """(relative file under ps2xRuntime/src/lib/, exact?)"""
        if self.has_repo and name in self.defined:
            return self.defined[name], True
        table = SYSCALL_NAME_FILES if syscall else STUB_PREFIX_FILES
        for pat, rel in table:
            if re.search(pat, name):
                return rel, False
        return None, False

    def syscall_name(self, n):
        repo = self.dispatch.get(n) if self.dispatch is not None else None
        return BUILTIN_SYSCALLS.get(n), repo


DEF_RE = re.compile(r"^\s*void\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*uint8_t\s*\*\s*[A-Za-z_]*\s*,\s*R5900Context\s*\*",
                    re.M)
TODO_RE = re.compile(r'TODO_NAMED\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')
CASE_RE = re.compile(r"case\s+(?:static_cast<\s*uint32_t\s*>\s*\(\s*)?(-?)\s*(0x[0-9A-Fa-f]+|\d+)\s*\)?\s*:")
CALL_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*::)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*rdram\s*,\s*ctx\s*,\s*runtime\s*\)\s*;")


def macro_names(text, macro):
    m = re.search(r"#define\s+%s\s*\(\s*X\s*\)" % macro, text)
    if not m:
        return set()
    names = []
    for line in text[m.end():].split("\n"):
        body = re.sub(r"/\*.*?\*/", "", line)
        names += re.findall(r"\bX\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", body)
        if not line.rstrip().endswith("\\"):
            break
    return set(names)


def parse_dispatcher(text):
    out = {}
    pending = []
    for line in text.split("\n"):
        code = line.split("//", 1)[0]
        for m in CASE_RE.finditer(code):
            v = int(m.group(2), 16) if m.group(2).lower().startswith("0x") else int(m.group(2))
            pending.append(-v if m.group(1) else v)
        c = CALL_RE.search(code)
        if c and pending:
            for n in pending:
                out.setdefault(n, c.group(1))
            pending = []
        if re.search(r"\bdefault\s*:", code):
            pending = []
    return out


# ---------------------------------------------------------------- log parsing

H = r"([0-9A-Fa-f]+)"
STUB_RE = re.compile(r"Warning: Unimplemented PS2 stub called\. name=(\S*) PC=0x" + H + r", RA=0x" + H)
SUPPRESS_RE = re.compile(r"Warning: Further calls to PS2 stub '([^']*)' are suppressed after (\d+) warnings")
SYSCALL_TODO_RE = re.compile(r"\[Syscall TODO\] encoded=0x" + H + r" v1=0x" + H + r" v0=0x" + H +
                             r".*? pc=0x" + H)
SYSCALL_WARN_RE = re.compile(r"Warning: Unimplemented PS2 syscall called\. PC=0x" + H + r", RA=0x" + H +
                             r", Encoded=0x" + H + r", v0=0x" + H + r", v1=0x" + H + r", Chosen=0x" + H)
UNKNOWN_HITS_RE = re.compile(r"-> Unknown syscallId=0x" + H + r" hits=(\d+)")
MISSING_RE = re.compile(r"Error: No exact recompiled function for guest PC 0x" + H + r" tableBase=0x" + H +
                        r" tableEnd=0x" + H + r" codeRegion=(yes|no)")
BRANCH_RE = re.compile(r"\[guest-branch:missing-target\] kind=(\S+) op=(\S+) source=0x" + H + r" target=0x" + H +
                       r" pc=0x" + H + r" ra=0x" + H)
CODE_REGION_RE = re.compile(r"codeRegion=(yes|no)")
RPC_RE = re.compile(r"\[IOP/RPC trace:unhandled\] sid=0x" + H + r" rpc=0x" + H + r" pc=0x" + H + r" ra=0x" + H)
RPC_MODS_RE = re.compile(r"loadedModules=\[([^\]]*)\]")
IOPLOG_RE = re.compile(r"\[ps2xIOP:(error|warning)\] (.*)")
SIFMOD_RE = re.compile(r'\[SIF module\] (\S+) id=(-?\d+) ref=(\d+) path="([^"]*)"')
OVR_UNRESOLVED_RE = re.compile(r"\[game_overrides\] unresolved handler '([^']*)' for address 0x" + H)
OVR_TABLE_RE = re.compile(r"\[function-table\] cannot replace guest PC 0x" + H)
OVR_APPLY_RE = re.compile(r"\[game_overrides\] applying '([^']*)'")
FATAL_RE = re.compile(r"(?:\[main\] fatal exception: |Error during program execution: |"
                      r"\[terminate\] std::exception: |\[terminate\] std::system_error .*?message=)(.*)")
UNHANDLED_RE = re.compile(r"(Unhandled [^\n]*?) at 0x" + H + r" raw=0x([0-9A-Fa-f]{8})")
ELF_LOADED_RE = re.compile(r"ELF file loaded successfully\. Entry point: 0x" + H)
CD_SEARCH_FAIL_RE = re.compile(r"sceCdSearchFile failed: (.*?) \((?:normalized path is empty, )?root: ([^,)]*)")
CD_LBN_RE = re.compile(r"sceCdRead unresolved LBN 0x" + H + r" sectors=(\d+)")
CD_REQ_RE = re.compile(r"\[sceCdRead\] unresolved request pc=0x" + H + r" ra=0x" + H)
FIO_RE = re.compile(r"\b(fio[A-Za-z]+) (error|warning): (.*)")


class Scan(object):
    def __init__(self):
        self.groups = {}
        self.order = 0
        self.lines = 0
        self.files = []
        self.milestones = {"argv_boot": False, "elf_entry": None, "overrides_applied": [],
                           "modules": [], "ended_with": None}

    def group(self, kind, key, where, line_text, pc=None, ra=None):
        k = (kind, key)
        g = self.groups.get(k)
        if g is None:
            self.order += 1
            g = {"kind": kind, "key": key, "rank": self.order, "first": where,
                 "first_pc": pc, "first_ra": ra, "example": line_text.strip()[:240],
                 "counts": {}, "extra": {}}
            self.groups[k] = g
        if g["first_pc"] is None and pc is not None:
            g["first_pc"] = pc
        if g["first_ra"] is None and ra is not None:
            g["first_ra"] = ra
        return g

    @staticmethod
    def bump(g, source, n=1):
        g["counts"][source] = g["counts"].get(source, 0) + n

    def feed(self, path, line_no, line):
        where = "%s:%d" % (os.path.basename(path), line_no)
        hit = False
        for m in STUB_RE.finditer(line):
            name = m.group(1) or "(unnamed)"
            g = self.group("stub", name, where, line, int(m.group(2), 16), int(m.group(3), 16))
            self.bump(g, "warn")
            hit = True
        for m in SUPPRESS_RE.finditer(line):
            g = self.group("stub", m.group(1) or "(unnamed)", where, line)
            g["extra"]["suppressed_after"] = int(m.group(2))
            hit = True
        for m in SYSCALL_WARN_RE.finditer(line):
            n = signed_syscall(int(m.group(6), 16))
            g = self.group("syscall", n, where, line, int(m.group(1), 16), int(m.group(2), 16))
            self.bump(g, "warn")
            hit = True
        if not hit:
            for m in SYSCALL_TODO_RE.finditer(line):
                enc, v1 = int(m.group(1), 16), int(m.group(2), 16)
                n = signed_syscall(enc if enc else v1)
                g = self.group("syscall", n, where, line, int(m.group(4), 16))
                self.bump(g, "todo")
                hit = True
            for m in UNKNOWN_HITS_RE.finditer(line):
                g = self.group("syscall", signed_syscall(int(m.group(1), 16)), where, line)
                g["counts"]["hits"] = max(g["counts"].get("hits", 0), int(m.group(2)))
                hit = True
        for m in MISSING_RE.finditer(line):
            pc = int(m.group(1), 16)
            g = self.group("missing", pc, where, line, pc)
            g["extra"]["code_region"] = m.group(4)
            g["extra"]["table"] = (int(m.group(2), 16), int(m.group(3), 16))
            self.bump(g, "dispatch")
            hit = True
        for m in BRANCH_RE.finditer(line):
            target = int(m.group(4), 16)
            g = self.group("missing", target, where, line, target, int(m.group(6), 16))
            g["extra"]["branch"] = {"kind": m.group(1), "op": m.group(2), "source": int(m.group(3), 16)}
            cr = CODE_REGION_RE.search(line)
            if cr:
                g["extra"].setdefault("code_region", cr.group(1))
            self.bump(g, "branch")
            hit = True
        for m in RPC_RE.finditer(line):
            sid, rpc = int(m.group(1), 16), int(m.group(2), 16)
            g = self.group("iop", "rpc sid=0x%X rpc=0x%X" % (sid, rpc), where, line,
                           int(m.group(3), 16), int(m.group(4), 16))
            g["extra"]["sid"] = sid
            mods = RPC_MODS_RE.search(line)
            if mods:
                g["extra"]["loaded_modules"] = mods.group(1)
            self.bump(g, "rpc")
            hit = True
        m = IOPLOG_RE.search(line)
        if m:
            msg = re.sub(r"0x[0-9A-Fa-f]+", "0x?", m.group(2).strip())
            msg = re.sub(r"\b\d+\b", "#", msg)[:160]
            g = self.group("iop", "ps2xIOP %s: %s" % (m.group(1), msg), where, line)
            g["extra"]["level"] = m.group(1)
            self.bump(g, "log")
            hit = True
        m = CD_SEARCH_FAIL_RE.search(line)
        if m:
            g = self.group("io", "disc file not found: %s" % m.group(1).strip()[:160], where, line)
            g["extra"].update({"path": m.group(1).strip(), "root": m.group(2).strip()})
            self.bump(g, "search")
            hit = True
        m = CD_LBN_RE.search(line)
        if m:
            g = self.group("io", "raw sector reads (no CD image)", where, line)
            g["extra"].setdefault("first_lbn", int(m.group(1), 16))
            self.bump(g, "lbn")
            hit = True
        m = CD_REQ_RE.search(line)
        if m:
            g = self.group("io", "sceCdRead request not understood", where, line,
                           int(m.group(1), 16), int(m.group(2), 16))
            self.bump(g, "req")
            hit = True
        m = FIO_RE.search(line)
        if m:
            msg = re.sub(r"\b\d+\b", "#", m.group(3).strip())[:160]
            g = self.group("io", "%s %s: %s" % (m.group(1), m.group(2), msg), where, line)
            g["extra"]["fio"] = m.group(1)
            self.bump(g, "fio")
            hit = True
        for m in OVR_UNRESOLVED_RE.finditer(line):
            g = self.group("override", "unresolved handler '%s'" % m.group(1), where, line, int(m.group(2), 16))
            g["extra"]["handler"] = m.group(1)
            self.bump(g, "log")
            hit = True
        for m in OVR_TABLE_RE.finditer(line):
            addr = int(m.group(1), 16)
            g = self.group("override", "outside function table 0x%08X" % addr, where, line, addr)
            self.bump(g, "log")
            hit = True
        m = FATAL_RE.search(line)
        if m:
            msg = m.group(1).strip()
            self.milestones["ended_with"] = msg[:240]
            u = UNHANDLED_RE.search(msg)
            stub_end = re.match(r"Unimplemented PS2 stub called: (\S+)", msg)
            if u:
                addr = int(u.group(2), 16)
                g = self.group("opcode", addr, where, line, addr)
                g["extra"]["what"] = u.group(1)
                g["extra"]["raw"] = u.group(3).upper()
                self.bump(g, "fatal")
            elif stub_end and ("stub", stub_end.group(1)) in self.groups:
                self.groups[("stub", stub_end.group(1))]["extra"]["ended_run"] = True
            else:
                key = re.sub(r"0x[0-9A-Fa-f]+", "0x?", msg)[:160]
                g = self.group("fatal", key, where, line)
                self.bump(g, "fatal")
            hit = True
        if not hit:
            if "Using argv boot path" in line:
                self.milestones["argv_boot"] = True
            m = ELF_LOADED_RE.search(line)
            if m:
                self.milestones["elf_entry"] = int(m.group(1), 16)
            for m in OVR_APPLY_RE.finditer(line):
                if m.group(1) not in self.milestones["overrides_applied"]:
                    self.milestones["overrides_applied"].append(m.group(1))
            for m in SIFMOD_RE.finditer(line):
                if len(self.milestones["modules"]) < 200:
                    self.milestones["modules"].append({"op": m.group(1), "id": int(m.group(2)), "path": m.group(4)})

    def read(self, path):
        if os.path.isdir(path):
            raise InputError("%s is a folder; pass log files" % path)
        try:
            f = open(path, "rb")
        except OSError as e:
            raise InputError("cannot open %s: %s" % (path, e.strerror or e))
        self.files.append(os.path.abspath(path))
        with f:
            for i, raw in enumerate(f, 1):
                self.lines += 1
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if len(line) > 20000:
                    line = line[:20000]
                self.feed(path, i, line)

    def results(self):
        out = []
        for g in sorted(self.groups.values(), key=lambda x: x["rank"]):
            calls = max(g["counts"].values()) if g["counts"] else 1
            g = dict(g)
            g["calls"] = calls
            out.append(g)
        return out


# ---------------------------------------------------------------- advice

def hexs(v):
    return None if v is None else "0x%08X" % v


def advise(g, kb):
    """Return (tool, where, how) for one issue group."""
    kind = g["kind"]
    note_guess = "" if kb.has_repo else " (a guess from the name: pass --repo to confirm)"
    if kind == "stub":
        name = g["key"]
        auto = AUTO_NAME_RE.match(name)
        rkind, handler = kb.resolve(name)
        if handler:
            rel, exact = kb.file_for(handler)
            where = RT + rel if rel else RT + "Kernel/"
            if handler in kb.todo:
                how = ("`%s` exists as a runtime handler but is still a TODO in %s. Implement it there: read "
                       "arguments with getRegU32(ctx, 4..7), return with setReturnU32/setReturnS32. Until then a "
                       "temporary \"ret0@0x<start>\" or \"ret1@0x<start>\" binding in the TOML can show how much "
                       "it matters (record it in STATE.md as TEMPORARY)." % (handler, where))
            else:
                how = ("`%s` has a working runtime handler in %s, so this call came from a `skip` entry (skipped "
                       "functions always log as unimplemented) or from an older build. Take it out of `skip` "
                       "(bind it in `stubs` instead) and re-run ps2_recomp, or just rebuild." % (handler, where))
                return "TOML", "console/<serial>.toml", how
            return "Runtime C++", where, how
        if auto:
            addr = int(auto.group(1), 16)
            how = ("A skipped or unbound function (the name carries its start, 0x%08X). If Ghidra shows it is an "
                   "SDK call the runtime implements, bind it in [general].stubs: \"<Handler>@0x%08X\" (check "
                   "names with `stubs.py handlers --repo`). If it is the game's own code, take it out of skip/"
                   "stubs and re-run ps2_recomp. To classify it first, a temporary \"ret0@0x%08X\" for one run."
                   % (addr, addr, addr))
            return "TOML", "console/<serial>.toml [general].stubs", how
        rel, exact = kb.file_for(name)
        if kb.has_repo:
            hint = ("No runtime handler is named `%s`. Find the matching one (`stubs.py handlers --repo ... "
                    "--grep <part>`) and bind it: \"<Handler>@0x<start>\". Or implement the body in a game "
                    "override with runtime.replaceFunction(0x<start>, fn), which needs no header change. Adding "
                    "a new name to PS2_STUB_LIST edits ps2_call_list.h: every generated file rebuilds, ask first."
                    % name)
        else:
            hint = ("`%s` may be a runtime handler that is still a TODO%s, or a library name the runtime does "
                    "not know. Pass --repo to tell which. Bind with \"<Handler>@0x<start>\" or implement it."
                    % (name, (" in " + RT + rel) if rel else ""))
        return "TOML or runtime C++", (RT + rel + note_guess) if rel else "console/<serial>.toml", hint
    if kind == "syscall":
        n = g["key"]
        builtin, repo_name = kb.syscall_name(n)
        name = repo_name or builtin
        if repo_name:
            return ("Rebuild", DISPATCHER,
                    "This checkout already dispatches %s to `%s`: the log came from an older build. Rebuild "
                    "(incremental) and run again." % (fmt_syscall(n), repo_name))
        if name:
            rel, exact = kb.file_for(name, syscall=True)
            defined = kb.has_repo and (name in kb.defined or kb.resolve(name)[1])
            where = DISPATCHER
            if defined:
                how = ("Syscall %s is %s and the handler exists (%s), but Dispatcher.cpp has no case for it. Add "
                       "`case %s:` calling it." % (fmt_syscall(n), name, RT + kb.defined.get(name, rel or "Kernel/"),
                                                    case_literal(n)))
            else:
                how = ("Syscall %s is %s (standard EE kernel table). Implement it in %s and add `case %s:` in "
                       "Dispatcher.cpp. The runtime returns 0 for unknown syscalls and carries on, so fix this "
                       "before chasing later symptoms." % (fmt_syscall(n), name,
                                                          (RT + rel + note_guess) if rel else RT + "Kernel/Syscalls/",
                                                          case_literal(n)))
            return "Runtime C++", where, how
        return ("Runtime C++", DISPATCHER,
                "Unknown syscall %s. Identify it first: in Ghidra, the value loaded into v1 before the syscall at "
                "PC; ps2tek and the ps2sdk kernel headers list the numbers. Then implement it and add `case %s:`. "
                "The runtime returns 0 meanwhile." % (fmt_syscall(n), case_literal(n)))
    if kind == "missing":
        pc = g["key"]
        region = g["extra"].get("code_region")
        table = g["extra"].get("table")
        if table and not (table[0] <= pc < table[1]):
            return ("Investigate", "the caller (RA, trace=)",
                    "0x%08X is outside the generated function table [0x%08X, 0x%08X): a bad pointer or a second "
                    "program. Look at the caller, not the target." % (pc, table[0], table[1]))
        if region == "yes":
            return ("Recompiler", "console/<serial>.toml [general].entry_points",
                    "A function start inside the ELF's code that the recompiler did not emit. Add \"0x%08X\" to "
                    "entry_points (or define the function in Ghidra and re-export), then re-run ps2_recomp. Every "
                    "re-run rebuilds the runner: batch several entry points per run." % pc)
        return ("Investigate", "the caller (RA, trace=)",
                "0x%08X is outside the loaded ELF's code: an overlay or second program loaded at runtime (it needs "
                "its own recompile), or a wild pointer. Decompile the caller at RA-8 before anything else." % pc)
    if kind == "opcode":
        return ("Game override", "console/patches/ (override module)",
                "The recompiler could not translate %s (word 0x%s) at 0x%08X and emitted a throw. Replace the "
                "function with a game override, or patch the instruction in [[patches.instructions]] if an "
                "equivalent exists. Record it under Known upstream issues and offer the user a minimal upstream "
                "report (address and word only)." % (g["extra"].get("what", "an instruction"),
                                                      g["extra"].get("raw", "?"), g["key"]))
    if kind == "iop":
        if "sid" in g["extra"]:
            return ("IOP", "ps2xIOP/ and the extracted disc's modules",
                    "No loaded IRX module and no generic service answers SID 0x%X. Check the game's IRX files are "
                    "in the extracted disc folder and loaded (see the [SIF module] lines). Generic HLE exists only "
                    "for MCSERV, LIBSD and DBCMAN (ps2xIOP/src/modules/). If the module did load, it may need IOP "
                    "imports or hardware the emulator lacks (ps2xIOP/src/emulator/)." % g["extra"]["sid"])
        return ("IOP", "ps2xIOP/src/emulator/",
                "Read the message: module loads, imports and IOP services live in ps2xIOP. A missing IRX file is "
                "an extraction or path-case problem, not a code problem.")
    if kind == "io":
        ex = g["extra"]
        if "path" in ex:
            return ("Disc files", "the extracted disc folder (cdRoot)",
                    "The game asked for %s and it is not under %s. Check the file is on the disc (`iso.py ls`), "
                    "that the extraction finished (`iso.py info` says complete), and that the run command passes "
                    "the ELF inside the extracted folder: cdrom0: is that ELF's folder." % (ex["path"], ex["root"] or
                                                                                          "the ELF's folder"))
        if "first_lbn" in ex:
            return ("Game override", "console/patches/ (override module)",
                    "The game reads disc sectors by number (its own file table), not by name, and no CD image is "
                    "configured, so the reads return nothing. Point the runtime at the user's own disc image from "
                    "the game override: `PS2Runtime::IoPaths p = PS2Runtime::getIoPaths(); p.cdImage = "
                    "\"/path/to/your.iso\"; PS2Runtime::setIoPaths(p);`. It must be a plain 2048-byte-sector "
                    "image (`iso.py plain` converts a raw .bin). Keep that path local: never share the image.")
        if "fio" in ex:
            return ("Runtime C++", RT + "Kernel/Syscalls/FileIO.cpp",
                    "A file call failed inside the runtime. Paths are resolved by ps2_vfs.cpp against cdRoot, "
                    "host and mc0 as written (case-sensitive on Linux). Check the path in the message first.")
        return ("Runtime C++", RT + "Kernel/Stubs/CD.cpp",
                "sceCdRead got arguments the runtime could not map to a file or sector. Read sceCdRead in CD.cpp, "
                "decompile the caller (RA-8) in Ghidra, and compare the arguments with PCSX2.")
    if kind == "override":
        if "handler" in g["extra"]:
            return ("Game override", "your override module",
                    "bindAddressHandler got a name that is not in PS2_SYSCALL_LIST or PS2_STUB_LIST: '%s'. Check "
                    "the spelling with `stubs.py handlers --repo`." % g["extra"]["handler"])
        return ("Game override", "your override module",
                "replaceFunction refused an address outside the generated function table. Use the function's "
                "start address in this ELF build.")
    return ("Investigate", "the lines before it",
            "The run ended with an exception. Read the lines just before it; the cause is usually the last "
            "stub, syscall or missing function logged.")


def case_literal(n):
    return "static_cast<uint32_t>(-0x%X)" % -n if n < 0 else "0x%X" % n


def label(g):
    k = g["kind"]
    if k == "syscall":
        return "syscall %s" % fmt_syscall(g["key"])
    if k == "missing":
        return "missing function 0x%08X" % g["key"]
    if k == "opcode":
        return "untranslated instruction at 0x%08X" % g["key"]
    return str(g["key"])


# ---------------------------------------------------------------- commands

def build_report(paths, kb):
    scan = Scan()
    for p in paths:
        scan.read(p)
    rows = []
    for g in scan.results():
        tool, where, how = advise(g, kb)
        row = {
            "rank": len(rows) + 1, "kind": g["kind"], "what": label(g), "calls": g["calls"],
            "first": g["first"], "first_pc": hexs(g["first_pc"]), "first_ra": hexs(g["first_ra"]),
            "fix_tool": tool, "where": where, "how": how, "example": g["example"],
        }
        if g["kind"] == "syscall":
            builtin, repo_name = kb.syscall_name(g["key"])
            row["syscall"] = {"number": g["key"], "builtin_name": builtin, "dispatcher": repo_name}
        if g["kind"] == "stub":
            if g["first_pc"] is not None and g["first_pc"] == g["first_ra"]:
                row["how"] += (" The logged PC equals RA because the generated wrapper sets pc = ra before the "
                               "handler runs: the call site is RA-8 (0x%08X)." % ((g["first_ra"] - 8) & 0xFFFFFFFF))
            row["ended_run"] = bool(g["extra"].get("ended_run"))
            if "suppressed_after" in g["extra"]:
                row["suppressed_after"] = g["extra"]["suppressed_after"]
                row["calls"] = max(row["calls"], g["extra"]["suppressed_after"] + 1)
        if g["kind"] == "missing":
            row["code_region"] = g["extra"].get("code_region")
        rows.append(row)
    ms = dict(scan.milestones)
    if ms["elf_entry"] is not None:
        ms["elf_entry"] = hexs(ms["elf_entry"])
    return {"logs": scan.files, "lines": scan.lines, "repo": kb.repo, "issues": rows, "milestones": ms}


def md_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def print_markdown(rep):
    print("| # | Kind | What | Calls | First PC | First RA | Fix tool | Where | Status |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rep["issues"]:
        print("| %d | %s | %s | %s | %s | %s | %s | %s | open |" % (
            r["rank"], r["kind"], md_cell(r["what"]), r["calls"], r["first_pc"] or "-", r["first_ra"] or "-",
            md_cell(r["fix_tool"]), md_cell(r["where"])))


def print_text(rep, limit):
    ms = rep["milestones"]
    print("Read %d lines from %d log(s)%s" % (rep["lines"], len(rep["logs"]),
                                           "; handler names from %s" % rep["repo"] if rep["repo"] else
                                           "; no --repo: built-in table and name guesses"))
    boot = []
    if ms["argv_boot"]:
        boot.append("argv boot path")
    if ms["elf_entry"]:
        boot.append("ELF loaded, entry %s" % ms["elf_entry"])
    if ms["overrides_applied"]:
        boot.append("overrides applied: %s" % ", ".join(ms["overrides_applied"]))
    if ms["modules"]:
        boot.append("%d IOP module loads" % len(ms["modules"]))
    print("Boot: %s" % ("; ".join(boot) if boot else "no boot milestones seen"))
    if ms["ended_with"]:
        print("Run ended with: %s" % ms["ended_with"])
    if not rep["issues"]:
        print("No stubs, syscalls, missing functions or IOP gaps in these logs.")
        return
    print("%d issue(s), in the order they first appeared (fix the first one first):" % len(rep["issues"]))
    for r in rep["issues"][:limit]:
        print("")
        flag = "  [ended the run]" if r.get("ended_run") else ""
        print("%d. %s: %s  (%s call%s, first at %s)%s" % (r["rank"], r["kind"], r["what"], r["calls"],
                                                         "" if r["calls"] == 1 else "s", r["first"], flag))
        if r["first_pc"] or r["first_ra"]:
            print("   PC %s  RA %s" % (r["first_pc"] or "-", r["first_ra"] or "-"))
        print("   Fix tool: %s  ->  %s" % (r["fix_tool"], r["where"]))
        print("   %s" % r["how"])
    if len(rep["issues"]) > limit:
        print("\n... %d more (use --limit or --json)" % (len(rep["issues"]) - limit))


def cmd_scan(args):
    kb = Knowledge(args.repo)
    rep = build_report(args.logs, kb)
    if args.json:
        print(json.dumps(rep, indent=2))
    elif args.markdown:
        print_markdown(rep)
    else:
        print_text(rep, args.limit)
    return 1 if rep["issues"] else 0


def cmd_handlers(args):
    kb = Knowledge(args.repo)
    by_name = {}
    for n, name in kb.dispatch.items():
        by_name.setdefault(name, []).append(n)
    rows = []
    for kind, names in (("syscall", kb.syscall_names), ("stub", kb.stub_names)):
        for name in sorted(names, key=str.lower):
            rows.append({"name": name, "kind": kind,
                         "numbers": [fmt_syscall(n) for n in sorted(by_name.get(name, []), key=abs)],
                         "file": (RT + kb.defined[name]) if name in kb.defined else None,
                         "todo": name in kb.todo})
    extra = sorted(set(by_name) - kb.syscall_names - kb.stub_names, key=str.lower)
    for name in extra:
        rows.append({"name": name, "kind": "dispatch-only",
                     "numbers": [fmt_syscall(n) for n in sorted(by_name[name], key=abs)],
                     "file": (RT + kb.defined[name]) if name in kb.defined else None, "todo": name in kb.todo})
    if args.grep:
        g = args.grep.lower()
        rows = [r for r in rows if g in r["name"].lower()]
    if args.kind != "all":
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.todo:
        rows = [r for r in rows if r["todo"]]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print("%-34s %-13s %-28s %s%s" % (r["name"], r["kind"], ", ".join(r["numbers"]) or "-",
                                          r["file"] or "-", "  (TODO)" if r["todo"] else ""))
    print("%d handler(s)%s" % (len(rows), "" if not args.grep else " matching %r" % args.grep))
    return 0


def cmd_syscall(args):
    n = signed_syscall(parse_int(args.number))
    kb = Knowledge(args.repo)
    builtin, repo_name = kb.syscall_name(n)
    out = {"number": n, "display": fmt_syscall(n), "builtin_name": builtin,
           "dispatcher": repo_name if kb.has_repo else None, "repo": kb.repo}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("Syscall      %s" % out["display"])
        print("Standard     %s" % (builtin + " (built-in EE kernel table)" if builtin else
                                   "not in the built-in table"))
        if kb.has_repo:
            print("Runtime      %s" % (("dispatched to %s in Dispatcher.cpp" % repo_name) if repo_name else
                                       "no case in Dispatcher.cpp: the runtime logs [Syscall TODO] and returns 0"))
        else:
            print("Runtime      pass --repo <PS2Recomp> to check Dispatcher.cpp")
    return 0 if (builtin or repo_name) else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="stubs.py",
        description="Turn PS2Recomp runtime logs into ranked backend fixes; list the runtime's handlers and syscalls.")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("scan", help="parse runtime logs and suggest the fix tool for each issue")
    s.add_argument("logs", nargs="+", help="runtime log files (stdout+stderr of ps2EntryRunner)")
    s.add_argument("--repo", metavar="PATH", help="PS2Recomp checkout: real handler names and syscall numbers")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--markdown", action="store_true", help="a table to paste into console/STATE.md")
    s.add_argument("--limit", type=int, default=25, help="issues to print in text mode (default 25)")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("handlers", help="list runtime handlers, their syscall numbers and files")
    s.add_argument("--repo", metavar="PATH", required=True, help="PS2Recomp checkout")
    s.add_argument("--kind", choices=["all", "syscall", "stub", "dispatch-only"], default="all")
    s.add_argument("--grep", metavar="TEXT", help="only names containing TEXT (case-insensitive)")
    s.add_argument("--todo", action="store_true", help="only handlers that are still TODO_NAMED stubs")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_handlers)

    s = sub.add_parser("syscall", help="name an EE kernel syscall number (0x3C, 60, -0x1A, 0xFFFFFFE6)")
    s.add_argument("number", help="the syscall number")
    s.add_argument("--repo", metavar="PATH", help="PS2Recomp checkout: also check Dispatcher.cpp")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_syscall)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # let a negative syscall number through argparse ("syscall -0x1A"): move it after "--"
    if argv and argv[0] == "syscall" and "--" not in argv:
        neg = [a for a in argv[1:] if re.match(r"^-(0[xX])?[0-9A-Fa-f]+$", a)]
        if len(neg) == 1:
            rest = [a for a in argv[1:] if a is not neg[0]]
            argv = ["syscall"] + rest + ["--", neg[0]]
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InputError as e:
        sys.stderr.write("stubs.py: %s\n" % e)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
