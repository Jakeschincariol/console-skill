"""Tests for console-tester: triage.py, run.py, abdiff.py (offline, synthesized logs and dumps)."""

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

from _load import load, ROOT

triage = load("console-tester", "triage")
run = load("console-tester", "run")
abdiff = load("console-tester", "abdiff")


def cli(mod, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# Lines in the exact formats ps2xRuntime prints (Kernel/Stubs/Unimplemented.cpp,
# Kernel/Syscalls/System.cpp, Kernel/Syscalls/RPC.cpp, ps2_runtime.cpp, main.cpp ...).
LOG_A = """Using argv boot path
ELF file loaded successfully. Entry point: 0x100008
[Syscall TODO] encoded=0x0 v1=0x3c v0=0x0 a0=0x0 a1=0x0 a2=0x0 a3=0x0 pc=0x100100
Warning: Unimplemented PS2 syscall called. PC=0x100100, RA=0x100200, Encoded=0x0, v0=0x0, v1=0x3c, Chosen=0x3c
  Args: $a0=0x0, $a1=0x0, $a2=0x0, $a3=0x0
  -> Unknown syscallId=0x3c hits=1
[Syscall TODO] encoded=0x0 v1=0x3c v0=0x0 a0=0x0 a1=0x0 a2=0x0 a3=0x0 pc=0x100100
Warning: Unimplemented PS2 syscall called. PC=0x100100, RA=0x100200, Encoded=0x0, v0=0x0, v1=0x3c, Chosen=0x3c
  Args: $a0=0x0, $a1=0x0, $a2=0x0, $a3=0x0
[IOP/RPC trace:unhandled] sid=0x80000597 rpc=0x1 pc=0x101000 ra=0x101100 send=0x0/0 recv=0x0/0 sendBytes=[] loadedModules=[SIO2MAN,PADMAN]
[SifLoadElfPart] failed path="cdrom0:\\MODULES\\LIBSD.IRX;1" host="/games/x/MODULES/LIBSD.IRX" reason=open failed
[sceCdRead] unresolved request pc=0x102000 ra=0x102100 a0=0x1 a1=0x2 a2=0x3
Error: No exact recompiled function for guest PC 0x123456 tableBase=0x0 tableEnd=0x1000000 codeRegion=yes trace=0x100000>0x100100
[guest-branch:missing-target] kind=IndirectJump op=dispatch source=0x0 target=0x123456 pc=0x123456 ra=0x100300 sp=0x1ffff00 gp=0x0 a0=0x0 a1=0x0 a2=0x0 a3=0x0 s0=0x0 s1=0x0 v0=0x0 v1=0x0 a0Readable=yes a0[0]=0x0 a0[4]=0x0 a0[8]=0x0 a0[c]=0x0 codeRegion=yes policy=0 trace=
Warning: Unimplemented PS2 stub called. name=sceMcInit PC=0x104000, RA=0x104100, Stub# guess (from $v0)=0x0
  Args: $a0=0x0, $a1=0x0, $a2=0x0, $a3=0x0
Error during program execution: Unimplemented PS2 stub called: sceMcInit
"""

LOG_B = """Using argv boot path
ELF file loaded successfully. Entry point: 0x100008
Warning: Unimplemented PS2 syscall called. PC=0x100100, RA=0x100200, Encoded=0x0, v0=0x0, v1=0x3c, Chosen=0x3c
  -> Unknown syscallId=0x3c hits=5000
[IOP/RPC trace:unhandled] sid=0x80000597 rpc=0x1 pc=0x101000 ra=0x101100 send=0x0/0 recv=0x0/0 sendBytes=[] loadedModules=[SIO2MAN]
Warning: Unimplemented PS2 stub called. name=sceGsSyncV PC=0x105000, RA=0x105100, Stub# guess (from $v0)=0x0
Error during program execution: Unimplemented PS2 stub called: sceGsSyncV
"""

MAP_CSV = """Name,Start,End,Size
sub_00100000,0x00100000,0x00100400,1024
main,0x00100100,0x00100180,128
sub_00104000,0x00104000,0x00104200,512
"""


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text, mode="w"):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(text)
        return path


# ---------------------------------------------------------------- triage

class TestTriage(Tmp):
    def scan(self, text):
        path = self.write("log.txt", text)
        t = triage.scan_logs([path])
        return t, [i.to_dict() for i in t.ranked()]

    def test_ranks_and_owners(self):
        t, issues = self.scan(LOG_A)
        keys = [d["key"] for d in issues]
        self.assertEqual(keys[0], "stub:sceMcInit")       # fatal: stopped the run
        self.assertTrue(issues[0]["fatal"])
        self.assertEqual(issues[0]["owner"], "/console-saves")
        self.assertIn("MemoryCard.cpp", issues[0]["details"]["likely_file"])
        self.assertEqual(keys[1], "missing:0x123456")
        self.assertEqual(issues[1]["owner"], "/console-reverse")
        self.assertEqual(issues[1]["count"], 1)            # lookup + branch lines are one event
        self.assertEqual(issues[1]["ra"], "0x00100300")
        self.assertIn("entry_points", issues[1]["next"])
        self.assertEqual(keys[2], "syscall:0x3c")
        self.assertEqual(issues[2]["count"], 2)
        self.assertEqual(issues[2]["owner"], "/console-backend")
        self.assertEqual(keys[3], "iop:0x80000597:0x1")
        self.assertIn("PADMAN", issues[3]["details"]["loaded_modules"])
        self.assertEqual({d["kind"] for d in issues[4:]}, {"io"})
        self.assertEqual(len(issues), 6)
        self.assertEqual(t.lines, LOG_A.count("\n"))

    def test_counts_from_hits_and_suppression(self):
        _, issues = self.scan(LOG_B + "Warning: Further calls to PS2 stub 'sceGsSyncV' are suppressed after 50 warnings\n")
        by = {d["key"]: d for d in issues}
        self.assertEqual(by["syscall:0x3c"]["count"], 5000)
        self.assertEqual(by["stub:sceGsSyncV"]["count"], 51)
        self.assertEqual(by["stub:sceGsSyncV"]["owner"], "/console-graphics")

    def test_function_map(self):
        mp = self.write("map.csv", MAP_CSV)
        fmap = triage.FunctionMap.load(mp)
        self.assertEqual(fmap.describe(0x100100), "main")          # narrowest record wins
        self.assertEqual(fmap.describe(0x100200), "sub_00100000+0x200")
        self.assertEqual(fmap.describe(0x100180), "sub_00100000+0x180")  # End is exclusive
        self.assertIsNone(fmap.describe(0x200000))
        self.assertIsNone(fmap.describe(0x0FFFFF))
        log = self.write("a.log", LOG_A)
        code, out, _ = cli(triage, "scan", log, "--map", mp, "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        by = {d["key"]: d for d in data["issues"]}
        self.assertEqual(by["syscall:0x3c"]["function"], "main")
        self.assertEqual(by["syscall:0x3c"]["caller"], "sub_00100000+0x200")
        self.assertEqual(by["stub:sceMcInit"]["function"], "sub_00104000")

    def test_bad_maps(self):
        log = self.write("a.log", LOG_A)
        bad = self.write("bad.csv", "Function,Addr\nfoo,1\n")
        self.assertEqual(cli(triage, "scan", log, "--map", bad)[0], 2)
        bad2 = self.write("bad2.csv", "Name,Start,End,Size\nfoo,zz,0x10,4\n")
        code, _, err = cli(triage, "scan", log, "--map", bad2)
        self.assertEqual(code, 2)
        self.assertIn("hex", err)
        self.assertEqual(cli(triage, "scan", log, "--map", os.path.join(self.tmp, "none.csv"))[0], 2)

    def test_text_and_markdown(self):
        log = self.write("a.log", LOG_A)
        code, out, _ = cli(triage, "scan", log)
        self.assertEqual(code, 1)
        self.assertIn("[FATAL]", out)
        self.assertIn("-> /console-saves", out)
        code, out, _ = cli(triage, "scan", log, "--markdown", "--top", "2")
        self.assertTrue(out.startswith("| # | issue |"))
        self.assertEqual(out.strip().count("\n"), 3)   # header, rule, two rows
        for row in out.strip().splitlines()[2:]:
            self.assertEqual(row.count(" | "), 6, row)

    def test_clean_log_and_input_errors(self):
        clean = self.write("clean.log", "Using argv boot path\nELF file loaded successfully. Entry point: 0x100008\n")
        code, out, _ = cli(triage, "scan", clean)
        self.assertEqual(code, 0)
        self.assertIn("0 distinct issue", out)
        self.assertEqual(cli(triage, "scan", os.path.join(self.tmp, "missing.log"))[0], 2)
        self.assertEqual(cli(triage, "scan", self.tmp)[0], 2)
        self.assertEqual(cli(triage)[0], 2)

    def test_other_lines(self):
        text = "\n".join([
            "[terminate] unhandled exception",
            "[terminate] std::exception: vector too long",
            "Failed to load ELF file: /x/SLUS_999.99",
            "[game_overrides] unresolved handler 'sceFooBar' for address 0x100000",
            "[function-table] cannot replace guest PC 0x2000000: outside generated dense table [0x0, 0x1000000)",
            "[MPEG] runtime built without FFmpeg; MPEG video decode is disabled.",
            "[MPEG] FFmpeg MPEG-2 decoder not found.",
            "[pad-replay] /x/s.txt line 3 is not 'frame buttons [lx ly rx ry]' in frame order; replay disabled",
            "fioOpen error: Invalid path address",
            "fioOpen error: Invalid path address",
            "ps2_stub fopen error: Failed to open '/g/DATA/X.BIN' with mode 'rb'. Error: No such file or directory",
            "Error: No exact recompiled function for guest PC 0x2000 tableBase=0x0 tableEnd=0x1000000 codeRegion=no trace=",
        ]) + "\n"
        _, issues = self.scan(text)
        by = {d["key"]: d for d in issues}
        term = by["fatal:terminate"]
        self.assertEqual(term["count"], 1)
        self.assertIn("vector too long", term["details"]["exception"])
        elf = [d for d in issues if d["title"].startswith("Failed to load ELF")][0]
        self.assertEqual(elf["owner"], "/console-reverse")
        self.assertEqual(by["mod:handler:sceFooBar"]["owner"], "/console-mod")
        self.assertEqual(by["mod:table:0x2000000"]["owner"], "/console-mod")
        self.assertEqual(by["build:ffmpeg"]["owner"], "/console-anywhere")
        self.assertEqual(by["input:replay"]["owner"], "/console-controller")
        fio = [d for d in issues if d["key"].startswith("io:fioOpen")][0]
        self.assertEqual(fio["count"], 2)
        fopen = [d for d in issues if d["key"].startswith("io:fopen")][0]
        self.assertIn("X.BIN", fopen["title"])
        self.assertIn("not code", by["missing:0x2000"]["next"])
        self.assertTrue(any(d["owner"] == "/console-graphics" for d in issues))

    def test_compare(self):
        old = self.write("old.log", LOG_A)
        new = self.write("new.log", LOG_B)
        result = triage.compare([old], [new])
        self.assertIn("stub:sceMcInit", [d["key"] for d in result["fixed"]])
        self.assertIn("syscall:0x3c", [d["key"] for d in result["still"]])
        self.assertEqual([d["key"] for d in result["new"]], ["stub:sceGsSyncV"])
        code, out, _ = cli(triage, "compare", old, new)
        self.assertEqual(code, 1)
        self.assertIn("NEW    unimplemented stub sceGsSyncV", out)
        self.assertIn("FIXED", out)
        code, out, _ = cli(triage, "compare", new, new, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["new"], [])
        code, out, _ = cli(triage, "compare", old, new, "--markdown")
        self.assertIn("### new (1)", out)

    def test_lookup(self):
        mp = self.write("map.csv", MAP_CSV)
        code, out, _ = cli(triage, "lookup", "0x100104", "0x104000", "--map", mp)
        self.assertEqual(code, 0)
        self.assertIn("main+0x4", out)
        code, out, _ = cli(triage, "lookup", "0x900000", "--map", mp, "--json")
        self.assertEqual(code, 1)
        self.assertIsNone(json.loads(out)[0]["function"])
        self.assertEqual(cli(triage, "lookup", "zz", "--map", mp)[0], 2)

    def test_multiple_logs_first_occurrence(self):
        a = self.write("1.log", LOG_B)
        b = self.write("2.log", LOG_A)
        t = triage.scan_logs([a, b])
        ranked = [i.to_dict() for i in t.ranked()]
        fatal = [d for d in ranked if d["fatal"]]
        self.assertEqual(fatal[0]["key"], "stub:sceGsSyncV")   # earlier log first
        self.assertEqual(fatal[0]["first_log"], a)


# ---------------------------------------------------------------- run.py

FAKE_RUNNER = r'''
import os, signal, subprocess, sys, time
mode = os.path.basename(sys.argv[1]).split(".")[0]
def out(s):
    sys.stdout.write(s + "\n"); sys.stdout.flush()
def err(s):
    sys.stderr.write(s + "\n"); sys.stderr.flush()
out("Using argv boot path")
out("ELF file loaded successfully. Entry point: 0x100008")
if mode == "pass":
    out("[title] reached the title screen")
    time.sleep(30)
elif mode == "hang":
    time.sleep(30)
elif mode == "quick":
    out("[title] reached the title screen")
    sys.exit(0)
elif mode == "early":
    sys.exit(0)
elif mode == "crash":
    err("Warning: Unimplemented PS2 stub called. name=sceCdInit PC=0x1000, RA=0x2000, Stub# guess (from $v0)=0x0")
    err("Error during program execution: Unimplemented PS2 stub called: sceCdInit")
    sys.exit(0)
elif mode == "missing":
    err("Error: No exact recompiled function for guest PC 0x123456 tableBase=0x0 tableEnd=0x1000000 codeRegion=yes trace=")
    sys.exit(0)
elif mode == "code":
    err("boom")
    sys.exit(3)
elif mode == "signal":
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)
elif mode == "flood":
    for i in range(5000):
        sys.stdout.write("line %d\n" % i)
    out("[title] after the flood")
    time.sleep(30)
elif mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with open(sys.argv[1] + ".pid", "w") as fh:
        fh.write(str(child.pid))
    out("[child] started")
    time.sleep(30)
elif mode == "replay":
    script = os.environ.get("PS2X_INPUT_SCRIPT")
    if script:
        err("[pad-replay] loaded 2 lines from " + script)
    out("[title] reached the title screen")
    sys.exit(0)
'''


@unittest.skipIf(os.name == "nt", "the fake runner uses a POSIX shebang")
class TestRun(Tmp):
    def setUp(self):
        super().setUp()
        self.runner = self.write("bin/ps2EntryRunner", "#!%s\n%s" % (sys.executable, FAKE_RUNNER))
        os.chmod(self.runner, os.stat(self.runner).st_mode | stat.S_IXUSR)
        self.out = os.path.join(self.tmp, "console", "runs")

    def elf(self, mode):
        return self.write("game/%s.elf" % mode, "")

    def boot(self, mode, *extra):
        code, out, err = cli(run, "boot", "--runner", self.runner, "--elf", self.elf(mode),
                             "--out", self.out, "--json", *extra)
        self.assertNotEqual(code, 2, err)
        return code, json.loads(out)

    def test_pass_with_expect_until(self):
        t0 = time.monotonic()
        code, s = self.boot("pass", "--seconds", "10", "--expect", r"\[title\]", "--until-expect")
        self.assertEqual((code, s["result"]), (0, "PASS"))
        self.assertTrue(s["stopped_on_pass"])
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual(s["expects"][0]["line"], 3)
        with open(os.path.join(self.out, "latest.json")) as fh:
            self.assertEqual(json.load(fh)["result"], "PASS")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "latest.log")))
        self.assertTrue(os.path.isfile(s["log"]))

    def test_pass_without_expect_runs_whole_timeout(self):
        code, s = self.boot("pass", "--seconds", "0.4")
        self.assertEqual(s["result"], "PASS")
        self.assertTrue(s["timed_out"])
        self.assertIsNone(s["exit_code"])

    def test_hang(self):
        code, s = self.boot("hang", "--seconds", "0.4", "--expect", "title")
        self.assertEqual((code, s["result"]), (1, "HANG"))
        self.assertIn("never saw: title", s["reason"])
        self.assertIsNotNone(s["silent_for"])

    def test_crash_by_fatal_line_even_with_exit_zero(self):
        code, s = self.boot("crash", "--seconds", "5")
        self.assertEqual((code, s["result"]), (1, "CRASH"))
        self.assertIn("Error during program execution", s["first_fatal"]["text"])
        self.assertEqual(s["counts"]["stub"], 1)
        self.assertEqual(s["exit_code"], 0)

    def test_crash_by_exit_code_and_signal(self):
        _, s = self.boot("code", "--seconds", "5")
        self.assertEqual(s["result"], "CRASH")
        self.assertIn("code 3", s["reason"])
        _, s = self.boot("signal", "--seconds", "5")
        self.assertEqual(s["result"], "CRASH")
        self.assertEqual(s["signal"], "SIGTERM")

    def test_crash_after_missing_function(self):
        _, s = self.boot("missing", "--seconds", "5")
        self.assertEqual(s["result"], "CRASH")
        self.assertIn("recompiler never emitted", s["reason"])

    def test_fail_when_exiting_early(self):
        _, s = self.boot("early", "--seconds", "5")
        self.assertEqual(s["result"], "FAIL")
        _, s = self.boot("early", "--seconds", "5", "--expect", "title")
        self.assertEqual(s["result"], "FAIL")
        _, s = self.boot("quick", "--seconds", "5", "--expect", "title")
        self.assertEqual(s["result"], "PASS")

    def test_huge_log_keeps_head_and_tail(self):
        _, s = self.boot("flood", "--seconds", "10", "--keep", "100", "--expect", "after the flood",
                         "--until-expect")
        self.assertEqual(s["result"], "PASS")       # the expect matched past the kept head
        self.assertEqual(s["lines"], 5003)
        self.assertEqual(s["omitted"], 5003 - 200)
        with open(s["log"]) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 201)
        self.assertIn("lines omitted", lines[100])
        self.assertEqual(lines[-1], "[title] after the flood")

    def test_kills_the_whole_tree(self):
        _, s = self.boot("child", "--seconds", "0.7", "--expect", "never")
        self.assertEqual(s["result"], "HANG")
        with open(self.elf("child") + ".pid") as fh:
            pid = int(fh.read())
        deadline = time.monotonic() + 3
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            except PermissionError:
                break
            time.sleep(0.05)
        self.assertFalse(alive, "the runner's child survived the timeout")

    def test_input_sets_replay_env(self):
        script = self.write("inputs/menu.txt", "0 -\n10 START\n")
        _, s = self.boot("replay", "--seconds", "5", "--input", script, "--expect", "title")
        self.assertEqual(s["result"], "PASS")
        self.assertIn("[pad-replay] loaded", s["replay"])
        self.assertEqual(s["notes"], [])
        _, s = self.boot("quick", "--seconds", "5", "--input", script, "--expect", "title")
        self.assertTrue(any("--replay" in n for n in s["notes"]))

    def test_text_output_and_last(self):
        code, out, _ = cli(run, "boot", "--runner", self.runner, "--elf", self.elf("crash"),
                           "--out", self.out, "--seconds", "5", "--tail-chars", "200")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("CRASH"))
        self.assertIn("last 200 characters", out)
        code, out, _ = cli(run, "last", "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("CRASH", out)
        self.assertEqual(cli(run, "last", "--out", os.path.join(self.tmp, "none"))[0], 2)

    def test_soak(self):
        code, out, _ = cli(run, "soak", "--runner", self.runner, "--elf", self.elf("quick"),
                           "--out", self.out, "--runs", "3", "--seconds", "5", "--expect", "title",
                           "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["totals"]["PASS"], 3)
        self.assertFalse(data["flaky"])
        self.assertTrue(os.path.isfile(data["file"]))
        self.assertEqual(len({r["log"] for r in data["results"]}), 3)   # one log per run
        code, out, _ = cli(run, "soak", "--runner", self.runner, "--elf", self.elf("crash"),
                           "--out", self.out, "--runs", "5", "--seconds", "5", "--stop-on-crash")
        self.assertEqual(code, 1)
        self.assertIn("CRASH 1", out)
        self.assertIn("x1  Error during program execution", out)

    def test_usage_errors(self):
        elf = self.elf("pass")
        for argv in (["--runner", os.path.join(self.tmp, "nope"), "--elf", elf],
                     ["--runner", self.runner, "--elf", os.path.join(self.tmp, "nope.elf")],
                     ["--runner", self.runner, "--elf", elf, "--seconds", "0"],
                     ["--runner", self.runner, "--elf", elf, "--expect", "("],
                     ["--runner", self.runner, "--elf", elf, "--input", os.path.join(self.tmp, "x.txt")],
                     ["--runner", self.runner, "--elf", elf, "--keep", "1"]):
            code, _, err = cli(run, "boot", "--out", self.out, *argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("error: "), err)
        noexec = self.write("bin/plain", "x")
        self.assertEqual(cli(run, "boot", "--runner", noexec, "--elf", elf, "--out", self.out)[0], 2)
        self.assertEqual(cli(run, "soak", "--runner", self.runner, "--elf", elf, "--out", self.out,
                             "--runs", "0")[0], 2)
        self.assertEqual(cli(run)[0], 2)


class TestClassify(unittest.TestCase):
    def cap(self, lines, expects=()):
        c = run.Capture(10, list(expects))
        for line in lines:
            c.add(line)
        return c

    def test_rules(self):
        c = self.cap(["ok"], ["title"])
        self.assertEqual(run.classify(True, False, -15, c, True)[0], "HANG")
        self.assertEqual(run.classify(False, False, 0, c, True)[0], "FAIL")
        c = self.cap(["[title]"], ["title"])
        self.assertEqual(run.classify(True, False, -15, c, True)[0], "PASS")
        self.assertEqual(run.classify(False, False, 0, c, True)[0], "PASS")
        self.assertEqual(run.classify(False, False, 139, c, True)[0], "CRASH")
        c = self.cap(["[title]", "[main] fatal exception: boom"], ["title"])
        self.assertEqual(run.classify(True, False, -15, c, True)[0], "CRASH")

    def test_capture_bounds(self):
        c = self.cap(["l%d" % i for i in range(25)])
        lines, omitted = c.lines()
        self.assertEqual(omitted, 5)
        self.assertEqual(lines[0], "l0")
        self.assertEqual(lines[-1], "l24")
        self.assertIn("5 lines omitted", lines[10])


# ---------------------------------------------------------------- abdiff

class TestAbdiffRegs(Tmp):
    def test_json_nested_aliases_and_width(self):
        a = self.write("a.json", json.dumps({
            "pc": "0x00100100",
            "gpr": {"r2": "0x0000000000000000_0000000000000005", "a0": "0x1000", "sp": 0x1FFFF00,
                    "ra": [0x00100200, 0, 0, 0]},
            "fpr": {"f0": 1.5}}))
        b = self.write("b.json", json.dumps({
            "pc": "0x100100", "v0": "0xFFFFFFFF00000005", "$a0": "0x1004", "sp": "01ffff00",
            "ra": "0x100200", "f0": "0x3fc00000", "k0": "0x1"}))
        code, out, _ = cli(abdiff, "regs", a, b, "--json")
        self.assertEqual(code, 1)
        r = json.loads(out)
        names = [d["name"] for d in r["differences"]]
        self.assertEqual(names, ["v0", "a0"])          # f0 1.5 == 0x3fc00000, ra words match
        self.assertEqual(r["first"]["name"], "v0")
        self.assertEqual(r["only_in_b"], ["k0"])
        code, out, _ = cli(abdiff, "regs", a, b, "--width", "32", "--json")
        self.assertEqual([d["name"] for d in json.loads(out)["differences"]], ["a0"])
        code, out, _ = cli(abdiff, "regs", a, b, "--width", "32", "--ignore", "a0")
        self.assertEqual(code, 0)
        self.assertIn("identical", out)
        code, out, _ = cli(abdiff, "regs", a, b, "--only", "sp,ra")
        self.assertEqual(code, 0)

    def test_text_dump_and_pc_hint(self):
        a = self.write("a.txt", "pc=0x00123456 ra=0x00100300 sp=0x01ffff00 v0=0x5 hits=7\n")
        b = self.write("b.log", "[guest-branch:missing-target] kind=IndirectJump op=dispatch source=0x0 "
                                "target=0x123456 pc=0x123460 ra=0x100300 sp=0x1ffff00 v0=0x5 "
                                "a0[0]=0x9 codeRegion=yes\n")
        code, out, _ = cli(abdiff, "regs", a, b, "--only", "pc,ra,sp,v0")
        self.assertEqual(code, 1)
        self.assertIn("first divergence: pc", out)
        self.assertIn("not from the same point", out)
        regs = abdiff.load_regs(b)
        self.assertNotIn("a0[0]", regs)
        self.assertEqual(regs["target"], 0x123456)

    def test_list_form_and_errors(self):
        a = self.write("a.json", json.dumps([{"name": "v0", "value": "0x1"}, {"name": "r29", "value": 16}]))
        b = self.write("b.json", json.dumps({"v0": 1, "sp": "0x10"}))
        self.assertEqual(cli(abdiff, "regs", a, b)[0], 0)
        bad = self.write("bad.json", "{oops")
        self.assertEqual(cli(abdiff, "regs", bad, b)[0], 2)
        empty = self.write("empty.txt", "nothing here\n")
        self.assertEqual(cli(abdiff, "regs", empty, b)[0], 2)
        badval = self.write("badval.json", json.dumps({"v0": "0xZZ"}))
        self.assertEqual(cli(abdiff, "regs", badval, b)[0], 2)
        self.assertEqual(cli(abdiff, "regs", a, b, "--width", "16")[0], 2)
        self.assertEqual(cli(abdiff, "regs", a, b, "--only", "t9")[0], 2)
        self.assertEqual(cli(abdiff, "regs", a, os.path.join(self.tmp, "none.json"))[0], 2)

    def test_canonical_names(self):
        self.assertEqual(abdiff.canonical("$R31"), "ra")
        self.assertEqual(abdiff.canonical("gpr[4]"), "a0")
        self.assertEqual(abdiff.canonical("s8"), "fp")
        self.assertEqual(abdiff.canonical("r0"), "zero")
        self.assertEqual(abdiff.canonical("PC"), "pc")


class TestAbdiffMem(Tmp):
    def dumps(self, a, b):
        return self.write("a.bin", a, "wb"), self.write("b.bin", b, "wb")

    def test_identical(self):
        a, b = self.dumps(bytes(1000), bytes(1000))
        code, out, _ = cli(abdiff, "mem", a, b, "--base", "0x100000")
        self.assertEqual(code, 0)
        self.assertIn("identical", out)

    def test_first_divergence_and_ranges(self):
        base = bytearray(4096)
        other = bytearray(base)
        other[0x105] = 0xAA
        other[0x10A] = 0xBB          # within the 16-byte gap: same range
        other[0x800:0x804] = b"\x01\x02\x03\x04"
        a, b = self.dumps(bytes(base), bytes(other))
        code, out, _ = cli(abdiff, "mem", a, b, "--base", "0x00100000", "--json")
        self.assertEqual(code, 1)
        r = json.loads(out)
        self.assertEqual(r["first"]["address"], "0x00100105")
        self.assertEqual(r["first"]["b_byte"], "0xaa")
        self.assertEqual(r["first"]["word_address"], "0x00100104")
        self.assertEqual(r["first"]["b_word"], "0x0000aa00")
        self.assertEqual(r["differing_bytes"], 6)
        self.assertEqual(r["ranges"], [
            {"start": "0x00100105", "end": "0x0010010b", "length": 6},
            {"start": "0x00100800", "end": "0x00100804", "length": 4}])
        code, out, _ = cli(abdiff, "mem", a, b, "--gap", "0", "--json")
        self.assertEqual(json.loads(out)["range_count"], 3)
        code, out, _ = cli(abdiff, "mem", a, b, "--gap", "0", "--max-ranges", "1", "--json")
        r = json.loads(out)
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["ranges"]), 1)
        code, out, _ = cli(abdiff, "mem", a, b, "--base", "0x100000")
        self.assertIn("first divergence at 0x00100105", out)

    def test_ignore_ranges(self):
        base = bytearray(256)
        other = bytearray(base)
        other[0x10] = 1
        other[0x80] = 2
        a, b = self.dumps(bytes(base), bytes(other))
        code, out, _ = cli(abdiff, "mem", a, b, "--base", "0x1000", "--ignore-range", "0x1010-0x1011", "--json")
        r = json.loads(out)
        self.assertEqual(r["first"]["address"], "0x00001080")
        code, _, _ = cli(abdiff, "mem", a, b, "--base", "0x1000", "--ignore-range", "0x1000-0x1100")
        self.assertEqual(code, 0)
        self.assertEqual(cli(abdiff, "mem", a, b, "--ignore-range", "0x20-0x10")[0], 2)
        self.assertEqual(cli(abdiff, "mem", a, b, "--ignore-range", "lots")[0], 2)

    def test_size_mismatch_is_fast_and_reported(self):
        a, b = self.dumps(bytes(3 * 1024 * 1024), b"")
        t0 = time.monotonic()
        code, out, _ = cli(abdiff, "mem", a, b, "--json")
        self.assertLess(time.monotonic() - t0, 3)
        r = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(r["differing_bytes"], 3 * 1024 * 1024)
        self.assertEqual(r["range_count"], 1)
        self.assertEqual(r["first"]["address"], "0x00000000")
        code, out, _ = cli(abdiff, "mem", a, b)
        self.assertIn("sizes differ", out)

    def test_bad_input(self):
        a, b = self.dumps(b"\x00", b"\x00")
        self.assertEqual(cli(abdiff, "mem", a, b, "--base", "banana")[0], 2)
        self.assertEqual(cli(abdiff, "mem", a, os.path.join(self.tmp, "none.bin"))[0], 2)
        self.assertEqual(cli(abdiff, "mem", a, self.tmp)[0], 2)
        self.assertEqual(cli(abdiff)[0], 2)


class TestDocs(unittest.TestCase):
    def test_skill_files_exist_and_no_em_dashes(self):
        folder = os.path.join(ROOT, "console-tester")
        with open(os.path.join(folder, "SKILL.md")) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: console-tester\n"))
        import re
        for rel in re.findall(r"`\$\{CLAUDE_SKILL_DIR\}/([^`\s]+)", text) + re.findall(r"\((reference/[^)]+)\)", text):
            self.assertTrue(os.path.exists(os.path.join(folder, rel)), rel)
        for root, _, files in os.walk(folder):
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    self.assertNotIn("\u2014", fh.read(), name)
        with open(os.path.join(folder, "reference", "test-protocol.md")) as fh:
            self.assertTrue(fh.readline().startswith(
                "Adapted from hkmodd/ps2-recomp-Agent-SKILL (MIT), updated for PS2Recomp as of October 2026."))


if __name__ == "__main__":
    unittest.main()
