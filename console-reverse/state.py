#!/usr/bin/env python3
"""Keep console/STATE.md, the project memory every /console-* skill reads first.

Subcommands:
  detect   look around (game workspace, extracted disc, PS2Recomp checkout, build dir) and report
  init     create console/STATE.md from templates/STATE.md (refuses to overwrite)
  design   create console/design.md from templates/design.md (refuses to overwrite)
  show     print the state (or one section); the session log is trimmed to its last lines
  set      record one field, e.g. the active runner command
  phase    move to a phase (PHASE_SETUP .. PHASE_IO_MENUS, or 0..5) and log it
  log      append a dated line to the session log
  strike   count failed attempts at one problem (circuit breaker: stop at 3)

Never lists the generated runner folder: it only counts its .cpp files.
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "templates", "STATE.md")
DESIGN_TEMPLATE = os.path.join(HERE, "templates", "design.md")
NOT_SET = "(not set)"

PHASES = [
    ("PHASE_SETUP", "toolchain", "PS2Recomp builds here (ps2_recomp, ps2_analyzer, ps2EntryRunner) and "
     "both workspaces are recorded"),
    ("PHASE_EXTRACT", "disc and ELF", "the disc is extracted (console/disc), the boot ELF is found, "
     "elf.py info is recorded and console/design.md is started"),
    ("PHASE_FUNCTION_MAP", "function map", "Ghidra exported the function map (or ps2_analyzer wrote a TOML) "
     "and the TOML is in console/"),
    ("PHASE_RECOMPILE", "recompile", "ps2_recomp finished, its report is read (unhandled instructions, "
     "warnings, errors) and the generated files are in the runtime"),
    ("PHASE_BUILD_RUN", "build and boot", "ps2EntryRunner builds with exit code 0 and the run gets past boot "
     "(first file reads or first frames)"),
    ("PHASE_IO_MENUS", "I/O and menus", "the game reads its files, reaches its menus and takes input; "
     "temporary ret0/ret1 stubs are replaced by real code"),
]

# key -> label in templates/STATE.md ("- Label: value")
FIELDS = [
    ("title", "Title"), ("serial", "Serial"), ("region", "Region"), ("elf_name", "Boot ELF name"),
    ("workspace", "Game workspace"), ("console_dir", "Console folder"), ("disc_image", "Disc image"),
    ("disc_dir", "Extracted disc (cdRoot)"), ("elf_path", "Boot ELF path"),
    ("ps2recomp", "PS2Recomp checkout"), ("toml", "Recompiler config (TOML)"),
    ("ghidra_csv", "Ghidra function map (CSV)"), ("recomp_output", "Generated C++ (TOML output)"),
    ("host", "Host"), ("build_dir", "Build dir"), ("generator", "Generator"), ("compiler", "C++ compiler"),
    ("build_type", "Build type"), ("runner_exe", "Runner executable"),
    ("runner_cpp_count", "Runner .cpp count (count only, never list)"), ("phase", "Phase"),
]
FIELD_LABELS = dict(FIELDS)
SETTABLE = [k for k, _ in FIELDS if k != "phase"] + ["runner_cmd"]

REGIONS = {
    "SLUS": "USA", "SCUS": "USA", "SLES": "Europe", "SCES": "Europe",
    "SLPS": "Japan", "SLPM": "Japan", "SCPS": "Japan", "SLKA": "Korea", "SCKA": "Korea", "SCAJ": "Asia",
}

GITIGNORE = """# Written by console-reverse state.py. Game data, and anything derived from it, stays on this machine.
# Only your own patches, overrides, notes and art belong in a repository.
disc/
output/
textures/
audio/
levels/
saves/
runs/
dist/
*.iso
*.bin
*.img
*.mdf
*.nrg
*.chd
*.cso
*.elf
*.irx
SL??_*
SC??_*
"""

DISC_IMAGE_EXT = (".iso", ".bin", ".img", ".mdf", ".nrg", ".chd", ".cso")
SKIP_DIRS = {"runner", "node_modules", ".git", "out", "build", "build64", "outvita", "__pycache__"}


class InputError(Exception):
    """Bad input: exit 2 with a one-line message."""


# ---------------------------------------------------------------- locating files

def find_state(workspace):
    if workspace:
        return os.path.join(os.path.abspath(workspace), "console", "STATE.md")
    cur = os.getcwd()
    while True:
        cand = os.path.join(cur, "console", "STATE.md")
        if os.path.isfile(cand):
            return cand
        if os.path.basename(cur) == "console" and os.path.isfile(os.path.join(cur, "STATE.md")):
            return os.path.join(cur, "STATE.md")
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.join(os.getcwd(), "console", "STATE.md")
        cur = parent


def read_state(path):
    if not os.path.isfile(path):
        raise InputError("no state file at %s: run `state.py init` first" % path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_state(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def find_ci(directory, name):
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for n in names:
        if n.lower() == name.lower():
            return os.path.join(directory, n)
    return None


# ---------------------------------------------------------------- detection

def parse_cnf(path):
    try:
        with open(path, "rb") as f:
            text = f.read(65536).decode("latin-1")
    except OSError:
        return None
    boot2 = None
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip().upper() == "BOOT2":
                boot2 = v.strip()
    if not boot2:
        return None
    leaf = re.split(r"[\\/:]", boot2)[-1]
    leaf = re.sub(r";\d+$", "", leaf)
    m = re.match(r"^([A-Za-z]{4})_(\d{3})\.(\d{2})$", leaf)
    serial = "%s-%s%s" % (m.group(1).upper(), m.group(2), m.group(3)) if m else None
    return {"boot2": boot2, "elf_name": leaf, "serial": serial,
            "region": REGIONS.get(serial[:4]) if serial else None}


def shallow_dirs(base):
    out = []
    try:
        for e in sorted(os.listdir(base)):
            p = os.path.join(base, e)
            if os.path.isdir(p) and not e.startswith(".") and e not in SKIP_DIRS:
                out.append(p)
    except OSError:
        pass
    return out


def read_cmake_cache(path):
    vals = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):[A-Z]+=(.*)$", line.rstrip("\r\n"))
                if m and m.group(1) in ("CMAKE_GENERATOR", "CMAKE_CXX_COMPILER", "CMAKE_BUILD_TYPE",
                                        "CMAKE_CONFIGURATION_TYPES", "PS2X_DEFAULT_BOOT_ELF"):
                    vals[m.group(1)] = m.group(2)
    except OSError:
        pass
    return vals


def read_toml_paths(path):
    """The `output` and `ghidra_output` strings of a PS2Recomp TOML (basic strings only)."""
    keys = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'^\s*(output|ghidra_output)\s*=\s*"((?:[^"\\]|\\.)*)"', line)
                if m and m.group(1) not in keys:
                    keys[m.group(1)] = re.sub(r"\\(.)", r"\1", m.group(2))
    except OSError:
        pass
    return keys


def is_ps2recomp(path):
    return bool(path) and os.path.isfile(os.path.join(path, "ps2xRuntime", "CMakeLists.txt")) and \
        os.path.isdir(os.path.join(path, "ps2xRecomp"))


def count_cpp(folder):
    """Count .cpp files without listing them (runner/ can hold 30,000+ files)."""
    n = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.endswith(".cpp"):
                    n += 1
    except OSError:
        return None
    return n


def find_binary(build, sub, name):
    cands = [os.path.join(build, sub, name), os.path.join(build, sub, name + ".exe")]
    for cfg in ("Debug", "Release", "RelWithDebInfo", "MinSizeRel"):
        cands.append(os.path.join(build, sub, cfg, name + ".exe"))
        cands.append(os.path.join(build, sub, cfg, name))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def detect(workspace, ps2recomp=None):
    ws = os.path.abspath(workspace or os.getcwd())
    console = os.path.join(ws, "console")
    r = {"workspace": ws, "console_dir": console,
         "state_exists": os.path.isfile(os.path.join(console, "STATE.md")),
         "host": "%s %s" % (platform.system() or "unknown", platform.machine() or ""),
         "disc_dir": None, "elf_name": None, "elf_path": None, "serial": None, "region": None,
         "disc_images": [], "tomls": [], "ghidra_csvs": [],
         "ps2recomp": None, "build_dirs": [], "build_dir": None, "generator": None, "compiler": None,
         "build_type": None, "runner_exe": None, "ps2_recomp": None, "ps2_analyzer": None,
         "runner_cpp_count": None, "tools": {}, "notes": []}
    # extracted disc: SYSTEM.CNF in console/disc, the workspace, console/, or one level down
    cands = [os.path.join(console, "disc"), ws, console] + shallow_dirs(ws) + shallow_dirs(console)
    seen = set()
    for d in cands:
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        cnf = find_ci(d, "SYSTEM.CNF")
        if cnf and os.path.isfile(cnf):
            info = parse_cnf(cnf)
            if info:
                r["disc_dir"] = d
                r["elf_name"] = info["elf_name"]
                r["serial"] = info["serial"]
                r["region"] = info["region"]
                elf = find_ci(d, info["elf_name"])
                if elf and os.path.isfile(elf):
                    r["elf_path"] = elf
                break
    for d in (ws, console):
        try:
            for e in sorted(os.listdir(d)):
                p = os.path.join(d, e)
                if not os.path.isfile(p):
                    continue
                low = e.lower()
                if low.endswith(DISC_IMAGE_EXT):
                    r["disc_images"].append(p)
                elif d == console and low.endswith(".toml"):
                    r["tomls"].append(p)
                elif d == console and low.endswith(".csv"):
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as f:
                            if f.readline().replace(" ", "").strip().lower() == "name,start,end,size":
                                r["ghidra_csvs"].append(p)
                    except OSError:
                        pass
        except OSError:
            pass
    r["recomp_output"] = None
    if r["tomls"]:
        keys = read_toml_paths(r["tomls"][0])
        r["recomp_output"] = keys.get("output")
        if not r["ghidra_csvs"] and keys.get("ghidra_output"):
            r["ghidra_csvs"].append(keys["ghidra_output"])
    # PS2Recomp checkout
    if ps2recomp:
        guesses = [ps2recomp]          # the user named it: check that one only
    else:
        guesses = []
        parent = os.path.dirname(ws)
        for base in (ws, parent):
            for name in ("PS2Recomp", "ps2recomp", "PS2RECOMP"):
                guesses.append(os.path.join(base, name))
        guesses.append(ws)
    for g in guesses:
        if g and is_ps2recomp(g):
            r["ps2recomp"] = os.path.abspath(g)
            break
    if ps2recomp and not r["ps2recomp"]:
        r["notes"].append("%s is not a PS2Recomp checkout (no ps2xRuntime/CMakeLists.txt)" % ps2recomp)
    repo = r["ps2recomp"]
    if repo:
        builds = []
        for rel in ("out/build", "build", "build64", "out", "outvita"):
            p = os.path.join(repo, *rel.split("/"))
            if os.path.isfile(os.path.join(p, "CMakeCache.txt")):
                builds.append(p)
            if rel == "out/build":
                for sub in shallow_dirs(p) if os.path.isdir(p) else []:
                    if os.path.isfile(os.path.join(sub, "CMakeCache.txt")):
                        builds.append(sub)
        r["build_dirs"] = builds
        if builds:
            b = builds[0]
            cache = read_cmake_cache(os.path.join(b, "CMakeCache.txt"))
            r["build_dir"] = b
            r["generator"] = cache.get("CMAKE_GENERATOR")
            r["compiler"] = cache.get("CMAKE_CXX_COMPILER")
            r["build_type"] = cache.get("CMAKE_BUILD_TYPE") or (
                "multi-config (%s)" % cache["CMAKE_CONFIGURATION_TYPES"]
                if cache.get("CMAKE_CONFIGURATION_TYPES") else None)
            r["runner_exe"] = find_binary(b, "ps2xRuntime", "ps2EntryRunner")
            r["ps2_recomp"] = find_binary(b, "ps2xRecomp", "ps2_recomp")
            r["ps2_analyzer"] = find_binary(b, "ps2xAnalyzer", "ps2_analyzer")
            if len(builds) > 1:
                r["notes"].append("more than one build dir: %s. Ask which one is real, never delete one."
                                  % ", ".join(builds))
        else:
            r["notes"].append("no configured build dir yet (no CMakeCache.txt): Phase 0 configures one")
        r["runner_cpp_count"] = count_cpp(os.path.join(repo, "ps2xRuntime", "src", "runner"))
    for tool in ("cmake", "ninja", "git", "c++", "clang++", "g++", "clang-cl", "cl", "pkg-config", "xcrun"):
        r["tools"][tool] = shutil.which(tool)
    if os.name == "nt" and not os.environ.get("VSINSTALLDIR") and not shutil.which("cl"):
        r["notes"].append("Windows: no Visual Studio developer environment (VSINSTALLDIR unset, cl not on PATH)")
    return r


def runner_cmd_for(d):
    if d.get("runner_exe") and d.get("elf_path"):
        return '"%s" "%s"' % (d["runner_exe"], d["elf_path"])
    return None


# ---------------------------------------------------------------- state edits

def set_field(text, key, value):
    if key == "runner_cmd":
        pat = re.compile(r"(## Active runner command\n```\n)(.*?)(\n```)", re.S)
        if not pat.search(text):
            raise InputError("STATE.md has no 'Active runner command' block")
        return pat.sub(lambda m: m.group(1) + value + m.group(3), text, count=1)
    label = FIELD_LABELS[key]
    pat = re.compile(r"^- %s: .*$" % re.escape(label), re.M)
    if not pat.search(text):
        raise InputError("STATE.md has no '- %s:' line" % label)
    return pat.sub(lambda m: "- %s: %s" % (label, value), text, count=1)


def get_fields(text):
    out = {}
    for key, label in FIELDS:
        m = re.search(r"^- %s: (.*)$" % re.escape(label), text, re.M)
        out[key] = m.group(1).strip() if m else None
    m = re.search(r"## Active runner command\n```\n(.*?)\n```", text, re.S)
    out["runner_cmd"] = m.group(1) if m else None
    return out


def section_bounds(text, name):
    m = re.search(r"^## %s[^\n]*\n" % re.escape(name), text, re.M | re.I)
    if not m:
        return None
    nxt = re.search(r"^## ", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return m.start(), m.end(), end


def append_to_section(text, name, line):
    b = section_bounds(text, name)
    if b is None:
        text = text.rstrip("\n") + "\n\n## %s\n" % name
        b = section_bounds(text, name)
    start, body_start, end = b
    body = text[body_start:end].rstrip("\n")
    body = (body + "\n" if body else "") + line + "\n"
    tail = text[end:]
    if tail:
        body += "\n"
    return text[:body_start] + body + tail


def add_log(text, message):
    message = " ".join(message.split())
    return append_to_section(text, "Session log", "- %s  %s" % (now_stamp(), message))


STRIKE_RE = re.compile(r"^- \[(?P<key>[^\]\n]+)\] (?P<n>\d+)/3 strikes?; last: (?P<note>.*)$", re.M)


def parse_phase(value):
    v = value.strip().upper()
    if v.isdigit():
        i = int(v)
        if 0 <= i < len(PHASES):
            return i
    if not v.startswith("PHASE_"):
        v = "PHASE_" + v
    for i, (name, _, _) in enumerate(PHASES):
        if name == v:
            return i
    raise InputError("unknown phase %r: use one of %s (or 0..5)" % (value, ", ".join(p[0] for p in PHASES)))


def phase_label(i):
    name, short, _ = PHASES[i]
    return "%s (%d of 5: %s)" % (name, i, short)


# ---------------------------------------------------------------- commands

def cmd_detect(args):
    d = detect(args.workspace, args.ps2recomp)
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    def show(label, v):
        print("%-22s %s" % (label, v if v not in (None, "", []) else "-"))
    show("Game workspace", d["workspace"])
    show("State file", os.path.join(d["console_dir"], "STATE.md") + (" (exists)" if d["state_exists"] else " (not yet)"))
    show("Host", d["host"])
    show("Extracted disc", d["disc_dir"])
    show("Boot ELF", d["elf_path"] or d["elf_name"])
    show("Serial", ("%s (%s)" % (d["serial"], d["region"])) if d["serial"] else None)
    show("Disc images", ", ".join(d["disc_images"]))
    show("TOML configs", ", ".join(d["tomls"]))
    show("Ghidra maps", ", ".join(d["ghidra_csvs"]))
    show("Generated C++", d["recomp_output"])
    show("PS2Recomp", d["ps2recomp"])
    show("Build dir", d["build_dir"])
    show("Generator", d["generator"])
    show("C++ compiler", d["compiler"])
    show("Build type", d["build_type"])
    show("ps2_recomp", d["ps2_recomp"])
    show("ps2_analyzer", d["ps2_analyzer"])
    show("ps2EntryRunner", d["runner_exe"])
    show("Runner .cpp files", d["runner_cpp_count"])
    show("Tools on PATH", ", ".join(k for k, v in sorted(d["tools"].items()) if v))
    for n in d["notes"]:
        print("Note: %s" % n)
    missing = [k for k in ("disc_dir", "elf_path", "ps2recomp", "build_dir") if not d[k]]
    if missing:
        print("Not found: %s. Ask the user once, then record it with `state.py set`." % ", ".join(missing))
    return 0


def cmd_init(args):
    path = find_state(args.workspace) if args.workspace else os.path.join(os.getcwd(), "console", "STATE.md")
    if os.path.exists(path):
        sys.stderr.write("state.py: %s already exists; read it (`state.py show`) instead of starting over\n" % path)
        return 1
    ws = os.path.dirname(os.path.dirname(path))
    d = detect(ws, args.ps2recomp)
    try:
        with open(TEMPLATE, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        raise InputError("the template %s is missing: reinstall the skill" % TEMPLATE)
    serial = args.serial or d["serial"]
    if serial:
        m = re.match(r"^([A-Za-z]{4})[-_ ]?(\d{3})\.?(\d{2})$", serial.strip())
        if not m:
            raise InputError("%r is not a PS2 serial (expected like SLUS-20312)" % serial)
        serial = "%s-%s%s" % (m.group(1).upper(), m.group(2), m.group(3))
    vals = {
        "TITLE": args.game or NOT_SET,
        "SERIAL": serial or NOT_SET,
        "REGION": REGIONS.get(serial[:4], "unknown prefix") if serial else NOT_SET,
        "ELF_NAME": d["elf_name"] or ("%s_%s.%s" % (serial[:4], serial[5:8], serial[8:]) if serial else NOT_SET),
        "WORKSPACE": ws,
        "CONSOLE_DIR": os.path.join(ws, "console"),
        "DISC_IMAGE": d["disc_images"][0] if d["disc_images"] else NOT_SET,
        "DISC_DIR": d["disc_dir"] or NOT_SET,
        "ELF_PATH": d["elf_path"] or NOT_SET,
        "PS2RECOMP": d["ps2recomp"] or NOT_SET,
        "TOML": d["tomls"][0] if d["tomls"] else NOT_SET,
        "GHIDRA_CSV": d["ghidra_csvs"][0] if d["ghidra_csvs"] else NOT_SET,
        "RECOMP_OUTPUT": d["recomp_output"] or NOT_SET,
        "HOST": d["host"],
        "BUILD_DIR": d["build_dir"] or NOT_SET,
        "GENERATOR": d["generator"] or NOT_SET,
        "COMPILER": d["compiler"] or NOT_SET,
        "BUILD_TYPE": d["build_type"] or NOT_SET,
        "RUNNER_EXE": d["runner_exe"] or NOT_SET,
        "RUNNER_CPP_COUNT": str(d["runner_cpp_count"]) if d["runner_cpp_count"] is not None else NOT_SET,
        "PHASE": phase_label(0),
        "RUNNER_CMD": runner_cmd_for(d) or "(not set: record it with `state.py set runner_cmd \"...\"`)",
    }
    for k, v in vals.items():
        text = text.replace("{{%s}}" % k, v)
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise InputError("template placeholders left unfilled: %s" % ", ".join(sorted(set(left))))
    text = add_log(text, "state file created by state.py init")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_state(path, text)
    gi = os.path.join(os.path.dirname(path), ".gitignore")
    wrote_gi = False
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8", newline="\n") as f:
            f.write(GITIGNORE)
        wrote_gi = True
    unknown = [k.lower() for k, v in vals.items() if v == NOT_SET]
    if args.json:
        print(json.dumps({"state": path, "gitignore_written": wrote_gi, "unknown": unknown}, indent=2))
    else:
        print("Created %s" % path)
        if wrote_gi:
            print("Created %s (keeps disc/, output/ and other game-derived folders out of git)" % gi)
        if unknown:
            print("Still unknown: %s" % ", ".join(unknown))
            print("Ask the user once, then record each with `state.py set <key> <value>`.")
    return 0


def cmd_design(args):
    state_path = find_state(args.workspace)
    text = read_state(state_path)
    fields = get_fields(text)
    path = os.path.join(os.path.dirname(state_path), "design.md")
    if os.path.exists(path):
        sys.stderr.write("state.py: %s already exists; edit it instead\n" % path)
        return 1
    with open(DESIGN_TEMPLATE, "r", encoding="utf-8") as f:
        body = f.read()
    body = body.replace("{{TITLE}}", fields.get("title") or NOT_SET)
    body = body.replace("{{SERIAL}}", fields.get("serial") or NOT_SET)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    write_state(state_path, add_log(text, "design.md created"))
    print("Created %s" % path)
    return 0


def cmd_show(args):
    path = find_state(args.workspace)
    text = read_state(path)
    if args.json:
        fields = get_fields(text)
        fields["state"] = path
        fields["strikes"] = [{"key": m.group("key"), "strikes": int(m.group("n")), "last": m.group("note")}
                             for m in STRIKE_RE.finditer(text)]
        print(json.dumps(fields, indent=2))
        return 0
    if args.section:
        b = section_bounds(text, args.section)
        if b is None:
            raise InputError("no section %r in %s" % (args.section, path))
        sys.stdout.write(text[b[0]:b[2]].rstrip("\n") + "\n")
        return 0
    b = section_bounds(text, "Session log")
    if b and not args.all:
        body = text[b[1]:b[2]].rstrip("\n").split("\n")
        body = [l for l in body if l.strip()]
        keep = body[-args.log_lines:] if args.log_lines > 0 else []
        head = text[:b[1]]
        note = "(%d earlier log lines hidden; --all shows them)\n" % (len(body) - len(keep)) \
            if len(body) > len(keep) else ""
        sys.stdout.write(head + note + "\n".join(keep) + ("\n" if keep else "") + text[b[2]:])
    else:
        sys.stdout.write(text)
    return 0


def cmd_set(args):
    key = args.key.lower().replace("-", "_")
    if key not in SETTABLE:
        raise InputError("unknown field %r; fields: %s" % (args.key, ", ".join(SETTABLE)))
    path = find_state(args.workspace)
    text = read_state(path)
    value = " ".join(args.value.split()) if key != "runner_cmd" else args.value.strip()
    if not value:
        raise InputError("empty value for %s" % key)
    text = set_field(text, key, value)
    if key == "runner_cmd":
        text = add_log(text, "active runner command set")
    write_state(path, text)
    print("%s = %s" % (key, value))
    return 0


def cmd_phase(args):
    i = parse_phase(args.phase)
    path = find_state(args.workspace)
    text = read_state(path)
    old = get_fields(text).get("phase")
    text = set_field(text, "phase", phase_label(i))
    text = add_log(text, "phase -> %s" % PHASES[i][0])
    write_state(path, text)
    print("Phase: %s (was %s)" % (phase_label(i), old or "unset"))
    print("Done when: %s." % PHASES[i][2])
    return 0


def cmd_log(args):
    msg = " ".join(args.message)
    if not msg.strip():
        raise InputError("empty log line")
    path = find_state(args.workspace)
    text = read_state(path)
    write_state(path, add_log(text, msg))
    print("Logged: %s" % " ".join(msg.split()))
    return 0


def cmd_strike(args):
    path = find_state(args.workspace)
    text = read_state(path)
    if args.list:
        rows = [(m.group("key"), int(m.group("n")), m.group("note")) for m in STRIKE_RE.finditer(text)]
        if not rows:
            print("No open strikes.")
        for k, n, note in rows:
            print("[%s] %d/3  last: %s" % (k, n, note))
        return 1 if any(n >= 3 for _, n, _ in rows) else 0
    if not args.key:
        raise InputError("name the problem: state.py strike \"PC 0x00123456 hang\" \"what you tried\"")
    key = " ".join(args.key.split())
    if "]" in key or "[" in key:
        raise InputError("the problem name cannot contain [ or ]")
    current = None
    for m in STRIKE_RE.finditer(text):
        if m.group("key") == key:
            current = m
            break
    if args.clear:
        if current is None:
            print("No strikes recorded for [%s]." % key)
            return 0
        text = text[:current.start()] + text[current.end() + 1:]
        text = add_log(text, "circuit breaker cleared for [%s]: %s" % (key, args.note or "fixed"))
        write_state(path, text)
        print("Cleared [%s]." % key)
        return 0
    n = (int(current.group("n")) if current else 0) + 1
    note = " ".join((args.note or "no note").split())
    line = "- [%s] %d/3 strike%s; last: %s (%s)" % (key, n, "" if n == 1 else "s", note, now_stamp())
    if current:
        text = text[:current.start()] + line + text[current.end():]
    else:
        text = append_to_section(text, "Circuit breaker", line)
    text = add_log(text, "strike %d/3 on [%s]: %s" % (n, key, note))
    write_state(path, text)
    if n >= 3:
        print("STRIKE %d of 3 on [%s]: STOP. Do not guess again." % (n, key))
        print("1. Re-read STATE.md and reference/guardrails.md (/console-reverse).")
        print("2. Load the reference for this subsystem, read the original MIPS in Ghidra.")
        print("3. A/B against PCSX2 if it is available: the first divergence is the bug.")
        print("4. Still stuck: write the user one specific question with what you know and tried.")
        return 1
    if n == 2:
        print("Strike 2 of 3 on [%s]. Before the next attempt, load the reference for this subsystem "
              "and re-read the original code. No third guess without new information." % key)
    else:
        print("Strike 1 of 3 on [%s]." % key)
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", metavar="PATH",
                        help="game workspace that holds console/ (default: search up from here)")
    p = argparse.ArgumentParser(
        prog="state.py",
        description="Keep console/STATE.md: the project memory every /console-* skill reads first.")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("detect", parents=[common], help="report what can be found without asking")
    s.add_argument("--ps2recomp", metavar="PATH", help="the PS2Recomp checkout, if you know it")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("init", parents=[common], help="create console/STATE.md (refuses to overwrite)")
    s.add_argument("--game", help="the game's title as it shipped")
    s.add_argument("--serial", help="disc serial, e.g. SLUS-20312")
    s.add_argument("--ps2recomp", metavar="PATH", help="the PS2Recomp checkout")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("design", parents=[common], help="create console/design.md from the template")
    s.set_defaults(func=cmd_design)

    s = sub.add_parser("show", parents=[common], help="print the state (session log trimmed)")
    s.add_argument("--section", help="print one section, e.g. 'Crashes' or 'Active runner command'")
    s.add_argument("--log-lines", type=int, default=15, help="session log lines to show (default 15)")
    s.add_argument("--all", action="store_true", help="print the whole file")
    s.add_argument("--json", action="store_true", help="fields, phase, runner command and strikes as JSON")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("set", parents=[common], help="record one field: %s" % ", ".join(SETTABLE))
    s.add_argument("key", help="field name, e.g. runner_cmd, build_dir, title")
    s.add_argument("value", help="the value (quote it)")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("phase", parents=[common], help="move to a phase and log it")
    s.add_argument("phase", help="PHASE_SETUP, PHASE_EXTRACT, PHASE_FUNCTION_MAP, PHASE_RECOMPILE, "
                                 "PHASE_BUILD_RUN, PHASE_IO_MENUS, or 0..5")
    s.set_defaults(func=cmd_phase)

    s = sub.add_parser("log", parents=[common], help="append a dated line to the session log")
    s.add_argument("message", nargs="+", help="what happened, in one line")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("strike", parents=[common], help="circuit breaker: count a failed fix (stop at 3)")
    s.add_argument("key", nargs="?", help="the problem, e.g. \"PC 0x00123456 hang\"")
    s.add_argument("note", nargs="?", help="what was tried this time")
    s.add_argument("--clear", action="store_true", help="the problem is fixed: remove its line")
    s.add_argument("--list", action="store_true", help="show open problems and their strikes")
    s.set_defaults(func=cmd_strike)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InputError as e:
        sys.stderr.write("state.py: %s\n" % e)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
