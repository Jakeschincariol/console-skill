"""Tests for console-backend: stubs.py and the SKILL.md contract.

Logs and the PS2Recomp checkout are synthesized here: the log lines use the runtime's exact wording,
and the fake checkout holds a few hand-made lines in the real files' shapes. No game data anywhere.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from _load import load, ROOT

SKILL = "console-backend"
stubs = load(SKILL, "stubs")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = stubs.main(list(argv))
    return code, out.getvalue(), err.getvalue()


LOG = """\
Using argv boot path
[SIF module] load-emulated id=1 ref=1 path="cdrom0:\\MODULES\\SIO2MAN.IRX;1"
ELF file loaded successfully. Entry point: 0x100008[game_overrides] applying 'test mod'
[Syscall TODO] encoded=0x0 v1=0x5b v0=0x0 a0=0x1 a1=0x2 a2=0x3 a3=0x4 pc=0x1234a0
Warning: Unimplemented PS2 syscall called. PC=0x1234a0, RA=0x100200, Encoded=0x0, v0=0x0, v1=0x5b, Chosen=0x5b
  Args: $a0=0x1, $a1=0x2, $a2=0x3, $a3=0x4
  -> Unknown syscallId=0x5b hits=1
[Syscall TODO] encoded=0x0 v1=0xffffffb7 v0=0x0 a0=0x1 a1=0x2 a2=0x3 a3=0x4 pc=0x1234b0
Warning: Unimplemented PS2 syscall called. PC=0x1234b0, RA=0x100210, Encoded=0x0, v0=0x0, v1=0xffffffb7, Chosen=0xffffffb7
[Syscall TODO] encoded=0x99 v1=0x0 v0=0x0 a0=0x1 a1=0x2 a2=0x3 a3=0x4 pc=0x1234c0
Warning: Unimplemented PS2 syscall called. PC=0x1234c0, RA=0x100220, Encoded=0x99, v0=0x0, v1=0x0, Chosen=0x99
  -> Unknown syscallId=0x99 hits=5000
[Syscall TODO] encoded=0x99 v1=0x0 v0=0x0 a0=0x1 a1=0x2 a2=0x3 a3=0x4 pc=0x1234c0
Warning: Unimplemented PS2 syscall called. PC=0x1234c0, RA=0x100220, Encoded=0x99, v0=0x0, v1=0x0, Chosen=0x99
Error: No exact recompiled function for guest PC 0x1d9410 tableBase=0x0 tableEnd=0x1000000 codeRegion=yes trace=0x100000,0x100008
[guest-branch:missing-target] kind=IndirectJump op=dispatch source=0x0 target=0x1d9410 pc=0x1d9410 ra=0x100300 sp=0x1ff0 codeRegion=yes policy=0 trace=x
Error: No exact recompiled function for guest PC 0x900000 tableBase=0x0 tableEnd=0x1000000 codeRegion=no trace=0x1
Error: No exact recompiled function for guest PC 0x3000000 tableBase=0x0 tableEnd=0x1000000 codeRegion=no trace=0x1
[IOP/RPC trace:unhandled] sid=0x80000597 rpc=0x1 pc=0x12a000 ra=0x12a100 send=0x0/0 recv=0x0/0 sendBytes=[] loadedModules=[SIO2MAN]
[ps2xIOP:error] import 12 missing from module 0x3 (sysmem)
[ps2xIOP:error] import 13 missing from module 0x3 (sysmem)
sceCdSearchFile failed: \\DATA\\LEVEL9.BIN;1 (root: /games/disc, repeat=1)
sceCdRead unresolved LBN 0x1f40 sectors=16 (no mapped file and no configured CD image)
sceCdRead unresolved LBN 0x1f50 sectors=16 (no mapped file and no configured CD image)
fioRead error: Invalid file descriptor 7
Warning: Unimplemented PS2 stub called. name=sceCdRI PC=0x200010, RA=0x200010, Stub# guess (from $v0)=0x0
  Args: $a0=0x0, $a1=0x0, $a2=0x0, $a3=0x0
Warning: Unimplemented PS2 stub called. name=sub_00123456 PC=0x200020, RA=0x200020, Stub# guess (from $v0)=0x0
Warning: Unimplemented PS2 stub called. name=printf PC=0x200030, RA=0x200030, Stub# guess (from $v0)=0x0
Warning: Unimplemented PS2 stub called. name=sceSifSomething PC=0x200050, RA=0x200058, Stub# guess (from $v0)=0x0
Warning: Further calls to PS2 stub 'sceSifSomething' are suppressed after 8 warnings
Warning: Unimplemented PS2 stub called. name=MyLibThing PC=0x200040, RA=0x200040, Stub# guess (from $v0)=0x0
[game_overrides] unresolved handler 'sceCdReed' for address 0x123456
[function-table] cannot replace guest PC 0x2000000: outside generated dense table [0x0, 0x1000000)
[main] fatal exception: Unimplemented PS2 stub called: MyLibThing
"""

OPCODE_LOG = "Error during program execution: Unhandled MMI0 instruction: function 0x1F at 0x10A2C0 raw=0x7000003C\n"
CLEAN_LOG = "Using argv boot path\nELF file loaded successfully. Entry point: 0x100008\nhello from the game\n"

CALL_LIST = r"""#pragma once

#include <cstdint>

#define PS2_SYSCALL_LIST(X)    \
    X(FlushCache)              \
    X(CreateThread)            \
                               \
    X(GetEntryAddress)         \
    X(SetAlarm)

// Stubs
#define PS2_STUB_LIST(X)                      \
    /* Std/Libc */                            \
    X(ret0)                                   \
    X(printf)                                 \
    X(sceCdRead)                              \
    X(sceCdRI)                                \
    X(_sceMcInit)                             \
    /* Game/middleware */

#define PS2_TEST_HOOK_LIST(X) \
    X(setPadOverrideState, (uint16_t buttons))
"""

DISPATCHER = r"""#include "Common.h"
namespace ps2_syscalls
{
    bool dispatchNumericSyscall(uint32_t syscallNumber, uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        switch (syscallNumber)
        {
        case 0x20:
            CreateThread(rdram, ctx, runtime);
            return true;
        case 0x5B:
            GetEntryAddress(rdram, ctx, runtime);
            return true;
        case 0x64:
        case static_cast<uint32_t>(-0x68):
            FlushCache(rdram, ctx, runtime);
            return true;
        case static_cast<uint32_t>(-0x49):
            iDeleteSema(rdram, ctx, runtime);
            return true;
        // case 0x77: commented out, must not count
        case 0x79:
            ps2_stubs::sceSifSetReg(rdram, ctx, runtime);
            return true;
        default:
            return false;
        }
    }
}
"""

KERNEL = {
    "Stubs/CD.cpp": """namespace ps2_stubs
{
    void sceCdRead(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        setReturnS32(ctx, 1);
    }

    void sceCdRI(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)
    {
        TODO_NAMED("sceCdRI", rdram, ctx, runtime);
    }
}
""",
    "Stubs/LibC.cpp": "namespace ps2_stubs {\n    void printf(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)\n    {\n    }\n}\n",
    "Stubs/SIF.cpp": "    void sceSifSetReg(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)\n    {\n    }\n",
    "Syscalls/Thread.cpp": "    void FlushCache(uint8_t *, R5900Context *ctx, PS2Runtime *)\n    {\n    }\n"
                           "    void CreateThread(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)\n    {\n    }\n",
    "Syscalls/Sync.cpp": "    void iDeleteSema(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime) {}\n"
                         "    void SetAlarm(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime) {}\n",
    "Syscalls/System.cpp": "    void GetEntryAddress(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime) {}\n",
}


def make_repo(root):
    def w(rel, text):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    w("ps2xRuntime/include/ps2_call_list.h", CALL_LIST)
    w("ps2xRuntime/src/lib/Kernel/Syscalls/Dispatcher.cpp", DISPATCHER)
    for rel, text in KERNEL.items():
        w("ps2xRuntime/src/lib/Kernel/" + rel, text)
    return root


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="console-backend-test-")
        self.repo = make_repo(os.path.join(self.tmp, "PS2Recomp"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log(self, text, name="run.log", mode="w"):
        p = os.path.join(self.tmp, name)
        with open(p, mode + ("b" if isinstance(text, bytes) else ""), **({} if isinstance(text, bytes) else
                                                                          {"encoding": "utf-8"})) as f:
            f.write(text)
        return p

    def scan_json(self, *extra, text=LOG):
        code, out, err = run("scan", self.log(text), "--json", *extra)
        return code, json.loads(out)

    def find(self, rep, kind, what_part):
        for r in rep["issues"]:
            if r["kind"] == kind and what_part in r["what"]:
                return r
        self.fail("no %s issue containing %r in %s" % (kind, what_part, [r["what"] for r in rep["issues"]]))


# ---------------------------------------------------------------- parsing

class ScanParsing(Tmp):
    def test_exit_codes(self):
        self.assertEqual(run("scan", self.log(LOG))[0], 1)
        code, out, _ = run("scan", self.log(CLEAN_LOG, "clean.log"))
        self.assertEqual(code, 0)
        self.assertIn("No stubs", out)
        self.assertIn("ELF loaded, entry 0x00100008", out)

    def test_order_is_first_appearance(self):
        code, rep = self.scan_json()
        kinds = [r["kind"] for r in rep["issues"]]
        self.assertEqual(kinds[0], "syscall")
        self.assertEqual([r["rank"] for r in rep["issues"]], list(range(1, len(kinds) + 1)))
        whats = [r["what"] for r in rep["issues"]]
        self.assertLess(whats.index("sceCdRI"), whats.index("MyLibThing"))

    def test_syscalls_counted_and_signed(self):
        code, rep = self.scan_json()
        s5b = self.find(rep, "syscall", "0x5B")
        self.assertEqual(s5b["calls"], 1)
        self.assertEqual(s5b["first_pc"], "0x001234A0")
        self.assertEqual(s5b["first_ra"], "0x00100200")
        neg = self.find(rep, "syscall", "-0x49")
        self.assertEqual(neg["syscall"]["number"], -0x49)
        self.assertIn("0xFFFFFFB7", neg["what"])
        s99 = self.find(rep, "syscall", "0x99")
        self.assertEqual(s99["calls"], 5000, "hits= counter wins over the two logged lines")
        self.assertEqual(len([r for r in rep["issues"] if r["kind"] == "syscall"]), 3)

    def test_syscall_todo_line_alone_is_enough(self):
        text = "[Syscall TODO] encoded=0x0 v1=0x3c v0=0x0 a0=0x1 a1=0x2 a2=0x3 a3=0x4 pc=0x1000\n" * 3
        code, rep = self.scan_json(text=text)
        r = rep["issues"][0]
        self.assertEqual(r["syscall"]["number"], 0x3C)
        self.assertEqual(r["syscall"]["builtin_name"], "SetupThread")
        self.assertEqual(r["calls"], 3)

    def test_missing_functions(self):
        code, rep = self.scan_json()
        m = self.find(rep, "missing", "0x001D9410")
        self.assertEqual(m["code_region"], "yes")
        self.assertEqual(m["calls"], 1, "the dispatch error and the branch report are one event")
        self.assertEqual(m["first_ra"], "0x00100300")
        self.assertEqual(m["fix_tool"], "Recompiler")
        self.assertIn('"0x001D9410"', m["how"])
        out = self.find(rep, "missing", "0x00900000")
        self.assertEqual(out["fix_tool"], "Investigate")
        self.assertIn("overlay", out["how"])
        far = self.find(rep, "missing", "0x03000000")
        self.assertIn("outside the generated function table", far["how"])

    def test_iop(self):
        code, rep = self.scan_json()
        rpc = self.find(rep, "iop", "sid=0x80000597")
        self.assertEqual(rpc["first_pc"], "0x0012A000")
        self.assertIn("MCSERV", rpc["how"])
        errs = [r for r in rep["issues"] if r["kind"] == "iop" and "ps2xIOP error" in r["what"]]
        self.assertEqual(len(errs), 1, "numbers are normalised so repeats group")
        self.assertEqual(errs[0]["calls"], 2)
        self.assertEqual(rep["milestones"]["modules"][0]["path"], "cdrom0:\\MODULES\\SIO2MAN.IRX;1")

    def test_disc_io(self):
        code, rep = self.scan_json()
        miss = self.find(rep, "io", "LEVEL9.BIN")
        self.assertIn("/games/disc", miss["how"])
        self.assertEqual(miss["fix_tool"], "Disc files")
        lbn = self.find(rep, "io", "raw sector reads")
        self.assertEqual(lbn["calls"], 2)
        self.assertIn("setIoPaths", lbn["how"])
        self.assertIn("iso.py plain", lbn["how"])
        fio = self.find(rep, "io", "fioRead error")
        self.assertIn("FileIO.cpp", fio["where"])

    def test_stubs_and_the_stop(self):
        code, rep = self.scan_json()
        last = self.find(rep, "stub", "MyLibThing")
        self.assertTrue(last["ended_run"])
        self.assertIn("RA-8 (0x00200038)", last["how"])
        sif = self.find(rep, "stub", "sceSifSomething")
        self.assertEqual(sif["suppressed_after"], 8)
        self.assertEqual(sif["calls"], 9)
        self.assertNotIn("RA-8", sif["how"], "PC differs from RA here")
        self.assertEqual(rep["milestones"]["ended_with"], "Unimplemented PS2 stub called: MyLibThing")
        self.assertFalse([r for r in rep["issues"] if r["kind"] == "fatal"], "the stop is folded into the stub")

    def test_overrides(self):
        code, rep = self.scan_json()
        u = self.find(rep, "override", "sceCdReed")
        self.assertEqual(u["first_pc"], "0x00123456")
        self.assertIn("spelling", u["how"])
        t = self.find(rep, "override", "outside function table")
        self.assertIn("start address", t["how"])
        self.assertEqual(rep["milestones"]["overrides_applied"], ["test mod"])

    def test_untranslated_instruction(self):
        code, rep = self.scan_json(text=OPCODE_LOG)
        r = rep["issues"][0]
        self.assertEqual(r["kind"], "opcode")
        self.assertEqual(r["what"], "untranslated instruction at 0x0010A2C0")
        self.assertIn("0x7000003C", r["how"])
        self.assertEqual(r["fix_tool"], "Game override")

    def test_other_fatal(self):
        code, rep = self.scan_json(text="[main] fatal exception: bad_alloc at 0x1234\n")
        self.assertEqual(rep["issues"][0]["kind"], "fatal")
        self.assertEqual(rep["issues"][0]["fix_tool"], "Investigate")

    def test_markdown_table(self):
        code, out, _ = run("scan", self.log(LOG), "--markdown")
        lines = out.strip().split("\n")
        self.assertTrue(lines[0].startswith("| # | Kind | What | Calls |"))
        self.assertEqual(lines[1].count("---"), 9)
        self.assertEqual(len(lines), 2 + len(json.loads(run("scan", self.log(LOG), "--json")[1])["issues"]))
        for line in lines[2:]:
            self.assertTrue(line.endswith("| open |"), line)

    def test_text_output_and_limit(self):
        code, out, _ = run("scan", self.log(LOG), "--limit", "2")
        self.assertIn("Fix tool:", out)
        self.assertIn("more (use --limit or --json)", out)
        self.assertIn("no --repo", out)
        self.assertIn("Run ended with", out)

    def test_several_logs_and_garbage(self):
        a = self.log(LOG, "a.log")
        b = self.log(b"\xff\xfe\x00garbage\n" + OPCODE_LOG.encode() + b"\x00" * 50000 + b"\n", "b.log")
        code, out, _ = run("scan", a, b, "--json")
        rep = json.loads(out)
        self.assertEqual(len(rep["logs"]), 2)
        self.assertTrue(any(r["kind"] == "opcode" for r in rep["issues"]))

    def test_bad_inputs(self):
        code, _, err = run("scan", os.path.join(self.tmp, "nope.log"))
        self.assertEqual(code, 2)
        self.assertIn("cannot open", err)
        self.assertEqual(run("scan", self.tmp)[0], 2)
        self.assertEqual(run("scan", self.log(LOG), "--repo", self.tmp)[0], 2)

    def test_big_log_is_fast(self):
        line = "Warning: Unimplemented PS2 stub called. name=sceCdRI PC=0x200010, RA=0x200010, Stub# guess (from $v0)=0x0\n"
        p = self.log("".join("noise %d\n%s" % (i, line) for i in range(30000)), "big.log")
        t = time.time()
        code, out, _ = run("scan", p, "--json")
        self.assertLess(time.time() - t, 4.0)
        rep = json.loads(out)
        self.assertEqual(rep["issues"][0]["calls"], 30000)
        self.assertEqual(rep["lines"], 60000)


# ---------------------------------------------------------------- the checkout

class RepoKnowledge(Tmp):
    def test_macro_and_dispatcher_parsing(self):
        names = stubs.macro_names(CALL_LIST, "PS2_STUB_LIST")
        self.assertEqual(names, {"ret0", "printf", "sceCdRead", "sceCdRI", "_sceMcInit"})
        self.assertEqual(stubs.macro_names(CALL_LIST, "PS2_SYSCALL_LIST"),
                         {"FlushCache", "CreateThread", "GetEntryAddress", "SetAlarm"})
        d = stubs.parse_dispatcher(DISPATCHER)
        self.assertEqual(d[0x64], "FlushCache")
        self.assertEqual(d[-0x68], "FlushCache")
        self.assertEqual(d[-0x49], "iDeleteSema")
        self.assertEqual(d[0x79], "sceSifSetReg")
        self.assertNotIn(0x77, d)

    def test_alias_with_leading_underscore(self):
        kb = stubs.Knowledge(self.repo)
        self.assertEqual(kb.resolve("sceMcInit"), ("stub", "_sceMcInit"))
        self.assertEqual(kb.resolve("_printf"), ("stub", "printf"))
        self.assertEqual(kb.resolve("nothing"), (None, None))
        self.assertEqual(kb.defined["FlushCache"], "Kernel/Syscalls/Thread.cpp")
        self.assertEqual(kb.todo, {"sceCdRI": "Kernel/Stubs/CD.cpp"})

    def test_scan_with_repo_names_exact_files(self):
        code, rep = self.scan_json("--repo", self.repo)
        ri = self.find(rep, "stub", "sceCdRI")
        self.assertEqual(ri["fix_tool"], "Runtime C++")
        self.assertEqual(ri["where"], "ps2xRuntime/src/lib/Kernel/Stubs/CD.cpp")
        self.assertIn("still a TODO", ri["how"])
        pf = self.find(rep, "stub", "printf")
        self.assertEqual(pf["fix_tool"], "TOML")
        self.assertIn("skip", pf["how"])
        sub = self.find(rep, "stub", "sub_00123456")
        self.assertIn('"ret0@0x00123456"', sub["how"])
        lib = self.find(rep, "stub", "MyLibThing")
        self.assertIn("replaceFunction", lib["how"])
        self.assertIn("ps2_call_list.h", lib["how"])
        s5b = self.find(rep, "syscall", "0x5B")
        self.assertEqual(s5b["fix_tool"], "Rebuild")
        self.assertEqual(s5b["syscall"]["dispatcher"], "GetEntryAddress")
        s99 = self.find(rep, "syscall", "0x99")
        self.assertIn("Unknown syscall", s99["how"])

    def test_known_syscall_with_handler_but_no_case(self):
        text = "Warning: Unimplemented PS2 syscall called. PC=0x10, RA=0x20, Encoded=0x18, v0=0x0, v1=0x0, Chosen=0x18\n"
        code, rep = self.scan_json("--repo", self.repo, text=text)
        r = rep["issues"][0]
        self.assertIn("SetAlarm", r["how"])
        self.assertIn("Add `case 0x18:`", r["how"])
        self.assertIn("Kernel/Syscalls/Sync.cpp", r["how"])

    def test_known_syscall_without_repo_guesses_file(self):
        text = "Warning: Unimplemented PS2 syscall called. PC=0x10, RA=0x20, Encoded=0x0, v0=0x0, v1=0xffffffbd, Chosen=0xffffffbd\n"
        code, rep = self.scan_json(text=text)
        r = rep["issues"][0]
        self.assertEqual(r["syscall"]["builtin_name"], "iSignalSema")
        self.assertIn("Kernel/Syscalls/Sync.cpp", r["how"])
        self.assertIn("pass --repo", r["how"])
        self.assertIn("static_cast<uint32_t>(-0x43)", r["how"])

    def test_stub_without_repo_guesses_file(self):
        text = "Warning: Unimplemented PS2 stub called. name=sceMcOpen PC=0x1, RA=0x1, Stub# guess (from $v0)=0x0\n"
        code, rep = self.scan_json(text=text)
        self.assertIn("Kernel/Stubs/MemoryCard.cpp", rep["issues"][0]["where"])

    def test_handlers_listing(self):
        code, out, _ = run("handlers", "--repo", self.repo, "--json")
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertEqual(rows["FlushCache"]["numbers"], ["0x64", "-0x68 (0xFFFFFF98)"])
        self.assertEqual(rows["FlushCache"]["file"], "ps2xRuntime/src/lib/Kernel/Syscalls/Thread.cpp")
        self.assertTrue(rows["sceCdRI"]["todo"])
        self.assertFalse(rows["sceCdRead"]["todo"])
        self.assertEqual(rows["iDeleteSema"]["kind"], "dispatch-only")
        code, out, _ = run("handlers", "--repo", self.repo, "--grep", "cdr", "--json")
        self.assertEqual(set(r["name"] for r in json.loads(out)), {"sceCdRead", "sceCdRI"})
        code, out, _ = run("handlers", "--repo", self.repo, "--todo")
        self.assertIn("sceCdRI", out)
        self.assertNotIn("sceCdRead ", out)
        code, out, _ = run("handlers", "--repo", self.repo, "--kind", "syscall", "--json")
        self.assertTrue(all(r["kind"] == "syscall" for r in json.loads(out)))

    def test_handlers_needs_a_real_repo(self):
        self.assertEqual(run("handlers", "--repo", self.tmp)[0], 2)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                stubs.main(["handlers"])
        self.assertEqual(cm.exception.code, 2)


class SyscallCommand(Tmp):
    def test_number_forms(self):
        for form in ("0x20", "32", "0X20"):
            code, out, _ = run("syscall", form, "--json")
            self.assertEqual(json.loads(out)["builtin_name"], "CreateThread", form)
        for form in ("-0x1A", "-26", "0xFFFFFFE6", "0xffffffe6"):
            code, out, _ = run("syscall", form, "--json")
            self.assertEqual(code, 0, form)
            info = json.loads(out)
            self.assertEqual(info["number"], -0x1A, form)
            self.assertEqual(info["builtin_name"], "iEnableIntc", form)

    def test_negative_number_with_options_in_any_order(self):
        code, out, _ = run("syscall", "--json", "-0x68", "--repo", self.repo)
        info = json.loads(out)
        self.assertEqual(info["dispatcher"], "FlushCache")
        self.assertEqual(info["builtin_name"], "iFlushCache")
        code, out, _ = run("syscall", "-0x49", "--repo", self.repo)
        self.assertEqual(code, 0)
        self.assertIn("iDeleteSema", out)
        self.assertIn("not in the built-in table", out)

    def test_unknown_and_bad(self):
        code, out, _ = run("syscall", "0x99")
        self.assertEqual(code, 1)
        self.assertIn("pass --repo", out)
        code, out, _ = run("syscall", "0x99", "--repo", self.repo)
        self.assertIn("[Syscall TODO]", out)
        code, _, err = run("syscall", "banana")
        self.assertEqual(code, 2)
        self.assertIn("not a number", err)

    def test_builtin_table_is_consistent(self):
        for n, name in stubs.BUILTIN_SYSCALLS.items():
            self.assertTrue(-0x80 < n < 0x80, n)
            self.assertRegex(name, r"^[A-Za-z][A-Za-z0-9]+$")
            if n < 0:
                self.assertTrue(name.startswith("i"), name)
        self.assertEqual(stubs.signed_syscall(0xFFFFFFB7), -0x49)
        self.assertEqual(stubs.fmt_syscall(-0x1A), "-0x1A (0xFFFFFFE6)")
        self.assertEqual(stubs.case_literal(-0x1A), "static_cast<uint32_t>(-0x1A)")


# ---------------------------------------------------------------- C++ snippets compile

MOCK_OVERRIDES_H = r"""#pragma once
#include <cstdint>
#include <string_view>
class PS2Runtime;
namespace ps2_game_overrides
{
    using ApplyFn = void (*)(PS2Runtime &runtime);
    struct Descriptor { const char *name = ""; const char *elfName = ""; uint32_t entry = 0; uint32_t crc32 = 0; ApplyFn apply = nullptr; };
    class AutoRegister { public: explicit AutoRegister(const Descriptor &descriptor) { (void)descriptor; } };
    bool bindAddressHandler(PS2Runtime &runtime, uint32_t address, std::string_view handlerName);
}
#define PS2_GAME_OVERRIDE_JOIN2(a, b) a##b
#define PS2_GAME_OVERRIDE_JOIN(a, b) PS2_GAME_OVERRIDE_JOIN2(a, b)
#define PS2_REGISTER_GAME_OVERRIDE(NAME_LITERAL, ELF_NAME_LITERAL, ENTRY_U32, CRC32_U32, APPLY_FN) \
    namespace { \
        static const ps2_game_overrides::Descriptor PS2_GAME_OVERRIDE_JOIN(kPs2OverrideDesc_, __LINE__) \
        { NAME_LITERAL, ELF_NAME_LITERAL, static_cast<uint32_t>(ENTRY_U32), static_cast<uint32_t>(CRC32_U32), APPLY_FN }; \
        static const ps2_game_overrides::AutoRegister PS2_GAME_OVERRIDE_JOIN(kPs2OverrideReg_, __LINE__) \
            (PS2_GAME_OVERRIDE_JOIN(kPs2OverrideDesc_, __LINE__)); \
    }
"""

MOCK_RUNTIME_H = r"""#pragma once
#include <cstdint>
#include <filesystem>
struct R5900Context { uint32_t pc = 0; };
class PS2Runtime
{
public:
    struct IoPaths
    {
        std::filesystem::path elfPath, elfDirectory, hostRoot, cdRoot, mcRoot, cdImage;
    };
    using RecompiledFunction = void (*)(uint8_t *, R5900Context *, PS2Runtime *);
    bool replaceFunction(uint32_t address, RecompiledFunction func);
    bool registerFunction(uint32_t address, RecompiledFunction func);
    RecompiledFunction lookupFunction(uint32_t address);
    static const IoPaths &getIoPaths();
    static void setIoPaths(const IoPaths &paths);
};
inline uint32_t getRegU32(const R5900Context *ctx, int reg) { (void)ctx; (void)reg; return 0; }
inline void setReturnU32(R5900Context *ctx, uint32_t value) { (void)ctx; (void)value; }
inline void setReturnS32(R5900Context *ctx, int32_t value) { (void)ctx; (void)value; }
inline uint8_t *getMemPtr(uint8_t *rdram, uint32_t addr) { return rdram ? rdram + (addr & 0x1FFFFFFu) : nullptr; }
"""


def cpp_blocks(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return [b for b in re.findall(r"```cpp\n(.*?)```", text, re.S) if "PS2_REGISTER_GAME_OVERRIDE(" in b]


@unittest.skipUnless(shutil.which("c++"), "no C++ compiler on PATH")
class SnippetsCompile(unittest.TestCase):
    def test_reference_snippets_compile_against_the_real_api_shape(self):
        blocks = cpp_blocks(os.path.join(ROOT, SKILL, "reference", "runtime-layers.md"))
        self.assertGreaterEqual(len(blocks), 2)
        tmp = tempfile.mkdtemp(prefix="console-backend-cpp-")
        try:
            with open(os.path.join(tmp, "game_overrides.h"), "w") as f:
                f.write(MOCK_OVERRIDES_H)
            with open(os.path.join(tmp, "ps2_runtime.h"), "w") as f:
                f.write(MOCK_RUNTIME_H)
            for i, block in enumerate(blocks):
                src = os.path.join(tmp, "snippet_%d.cpp" % i)
                with open(src, "w") as f:
                    f.write(block)
                r = subprocess.run(["c++", "-std=c++20", "-fsyntax-only", "-Wall", "-I", tmp, src],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace")[-1500:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- SKILL.md and CLI contract

class SkillContract(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, SKILL, "SKILL.md"), encoding="utf-8") as f:
            self.text = f.read()

    def test_frontmatter(self):
        m = re.match(r"^---\nname: console-backend\ndescription: >-\n(.*?)\nargument-hint: \"[^\"]+\"\n---\n",
                     self.text, re.S)
        self.assertIsNotNone(m)
        self.assertIn("Backend Engineer", m.group(1))
        self.assertIn("syscalls, kernel stubs, file\n  and disc I/O, the IOP", m.group(1))

    def test_length_links_and_tool_named(self):
        self.assertLess(len(self.text.splitlines()), 260)
        self.assertIn("stubs.py", self.text)
        for rel in re.findall(r"`((?:reference|templates)/[A-Za-z0-9_.-]+)`", self.text):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, SKILL, rel)), rel)
        for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9_./-]+)", self.text):
            self.assertTrue(os.path.exists(os.path.join(ROOT, SKILL, rel)), rel)
        for f in os.listdir(os.path.join(ROOT, SKILL, "reference")):
            self.assertIn("reference/" + f, self.text)

    def test_guard_rails_and_no_em_dashes(self):
        low = self.text.lower()
        self.assertIn("guard rails", low)
        self.assertIn("own", low)
        self.assertIn("publish", low)
        for base, _, files in os.walk(os.path.join(ROOT, SKILL)):
            for f in files:
                if f.endswith((".md", ".py")):
                    with open(os.path.join(base, f), encoding="utf-8") as fh:
                        self.assertNotIn("\u2014", fh.read(), f)
        with open(__file__, encoding="utf-8") as fh:
            self.assertNotIn("\u2014", fh.read())

    def test_reference_credits_the_base(self):
        with open(os.path.join(ROOT, SKILL, "reference", "runtime-layers.md"), encoding="utf-8") as fh:
            self.assertIn("Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of "
                          "October 2026.", fh.read())

    def test_tool_never_names_another_skill_folder(self):
        with open(os.path.join(ROOT, SKILL, "stubs.py"), encoding="utf-8") as fh:
            src = fh.read()
        for other in ("console-reverse", "console-tester", "console-mod", "console-levels"):
            self.assertNotIn(other + "/", src)

    def test_every_subcommand_help_runs(self):
        path = os.path.join(ROOT, SKILL, "stubs.py")
        for argv in (["--help"], ["scan", "--help"], ["handlers", "--help"], ["syscall", "--help"]):
            r = subprocess.run([sys.executable, path] + argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=30)
            self.assertEqual(r.returncode, 0, "%s: %s" % (argv, r.stderr[-300:]))
            self.assertIn(b"usage", r.stdout.lower())
        r = subprocess.run([sys.executable, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn(b"Traceback", r.stderr)
        r = subprocess.run([sys.executable, path, "syscall", "-0x1A"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"iEnableIntc", r.stdout)


if __name__ == "__main__":
    unittest.main()
