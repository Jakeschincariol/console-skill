#!/usr/bin/env python3
"""override.py: scaffold a PS2Recomp game override module that uses the REAL API.

  new   writes console/patches/<slug>.cpp: a PS2_REGISTER_GAME_OVERRIDE module keyed
        by your ELF's file name, entry point and CRC-32, with
          --bind    0xADDR=handler   route a game function to a runtime syscall/stub handler
          --replace 0xADDR=fn_name   replace a game function with your C++ (stub to fill in)
          --wrap    0xADDR=fn_name   run the game's function, then adjust what it did
  keys  prints the three keys (file name, entry, CRC-32) for a hand-written override

The template is templates/override.cpp (the corrected version of the original agent
skill's broken template). Python 3.8+, standard library only.
"""

import argparse
import datetime
import os
import re
import struct
import sys
import zlib

TOOL = "override.py"
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "templates", "override.cpp")
CMAKE_TEMPLATE = os.path.join(HERE, "templates", "console_mods.cmake")
PLACEHOLDERS = ("MOD_NAME", "ELF_NAME", "ENTRY", "CRC32", "NAMESPACE", "NOTES", "FUNCTIONS", "APPLY_BODY")
GENERIC_HANDLERS = ("ret0", "ret1", "reta0")
CPP_KEYWORDS = set("""alignas alignof and asm auto bitand bitor bool break case catch char class compl concept
const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype default delete do
double dynamic_cast else enum explicit export extern false float for friend goto if inline int long mutable
namespace new noexcept not nullptr operator or private protected public register reinterpret_cast requires
return short signed sizeof static static_assert static_cast struct switch template this thread_local throw true
try typedef typeid typename union unsigned using virtual void volatile wchar_t while xor main
applyOverride rdram ctx runtime""".split())


class UsageError(Exception):
    """Bad input the user can fix: printed as one line, exit code 2."""


def fail(msg):
    raise UsageError(msg)


def hex32(v):
    return "0x%08X" % (v & 0xFFFFFFFF)


def c_string(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def slugify(text):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not s:
        s = "mod"
    if s[0].isdigit():
        s = "m_" + s
    return s


# --------------------------------------------------------------------------
# Minimal ELF reader: the override keys and the executable segments
# --------------------------------------------------------------------------


class ElfKeys(object):
    def __init__(self, path):
        if not os.path.isfile(path):
            fail("ELF not found: %s" % path)
        with open(path, "rb") as f:
            data = f.read()
        self.name = os.path.basename(path)
        if len(data) < 52 or data[:4] != b"\x7fELF":
            fail("%s is not an ELF file" % self.name)
        if data[4] != 1 or data[5] != 1:
            fail("%s is not a 32-bit little-endian ELF (PS2 EE ELFs are)" % self.name)
        machine = struct.unpack_from("<H", data, 18)[0]
        if machine != 8:
            fail("%s is not a MIPS ELF (machine %d, the PS2 EE is 8)" % (self.name, machine))
        self.entry = struct.unpack_from("<I", data, 24)[0]
        phoff = struct.unpack_from("<I", data, 28)[0]
        phentsize, phnum = struct.unpack_from("<HH", data, 42)
        if phnum and (phentsize < 32 or phoff + phnum * phentsize > len(data)):
            fail("%s is truncated or has a bad program header table" % self.name)
        self.exec_ranges = []
        self.exec_segments = []
        for i in range(phnum):
            p_type, off, vaddr, _pa, filesz, _memsz, flags, _al = struct.unpack_from("<IIIIIIII", data, phoff + i * phentsize)
            if p_type == 1 and flags & 1 and filesz:
                if off + filesz > len(data):
                    fail("%s is truncated: segment %d runs past the end of the file" % (self.name, i))
                self.exec_ranges.append((vaddr, vaddr + filesz))
                self.exec_segments.append((vaddr, off, filesz))
        self.data = data
        self.crc32 = zlib.crc32(data) & 0xFFFFFFFF
        self.size = len(data)

    def in_code(self, addr):
        return any(a <= addr < b for a, b in self.exec_ranges)

    def call_sites(self, target):
        """jal and j instructions that reach `target` directly (executable segments)."""
        out = {"jal": [], "j": []}
        index = (target >> 2) & 0x03FFFFFF
        for base, off, size in self.exec_segments:
            if (base + 4) & 0xF0000000 != target & 0xF0000000:
                continue
            for kind, op in (("jal", 3), ("j", 2)):
                needle = struct.pack("<I", (op << 26) | index)
                i = self.data.find(needle, off, off + size)
                while i >= 0:
                    if (i - off) % 4 == 0:
                        out[kind].append(base + i - off)
                    i = self.data.find(needle, i + 1, off + size)
        return out


def function_starts(path):
    """Start addresses from a Ghidra CSV (Name,Start,End,Size)."""
    if not os.path.isfile(path):
        fail("Ghidra map not found: %s" % path)
    starts = set()
    with open(path, "r", errors="replace") as f:
        for line in f:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[1].lower().startswith("0x"):
                try:
                    starts.add(int(parts[1], 16))
                except ValueError:
                    pass
    return starts


def repo_handlers(repo):
    """Handler names from ps2xRuntime/include/ps2_call_list.h in a PS2Recomp checkout."""
    path = os.path.join(repo, "ps2xRuntime", "include", "ps2_call_list.h")
    if not os.path.isfile(path):
        fail("no ps2xRuntime/include/ps2_call_list.h under %s (is that a PS2Recomp checkout?)" % repo)
    with open(path, "r", errors="replace") as f:
        text = f.read()
    names = set()
    for block in re.split(r"#define\s+", text)[1:]:
        if block.startswith("PS2_SYSCALL_LIST") or block.startswith("PS2_STUB_LIST"):
            names.update(re.findall(r"\bX\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", block))
    if not names:
        fail("found no X(Name) entries in %s" % path)
    return names


def resolve_handler(name, names):
    """Same alias rule as ps2_runtime_calls.h: exact, or with/without one leading underscore."""
    if name in names:
        return name
    if name.startswith("_") and name[1:] in names:
        return name[1:]
    if ("_" + name) in names:
        return "_" + name
    return None


# --------------------------------------------------------------------------
# Rendering (also used by pnach.py override)
# --------------------------------------------------------------------------


def render_module(mod_name, elf_name, entry, crc32, slug, functions, apply_body, notes):
    if not os.path.isfile(TEMPLATE):
        fail("templates/override.cpp is missing (reinstall the console-mod skill)")
    with open(TEMPLATE, "r") as f:
        text = f.read()
    note_lines = "\n".join("// " + n if n else "//" for n in notes)
    values = {
        "MOD_NAME": c_string(mod_name),
        "ELF_NAME": c_string(elf_name),
        "ENTRY": "0x%08Xu" % entry,
        "CRC32": "0x%08Xu" % crc32,
        "NAMESPACE": "console_mod_" + slug,
        "NOTES": note_lines,
        "FUNCTIONS": functions,
        "APPLY_BODY": apply_body,
    }
    for k in PLACEHOLDERS:
        text = text.replace("@%s@" % k, values[k])
    left = re.findall(r"@[A-Z][A-Z0-9_]*@", text)
    if left:
        fail("template still has unfilled fields: %s" % ", ".join(sorted(set(left))))
    return text


def ensure_cmake_include(folder):
    """Put console_mods.cmake next to the overrides. Returns install advice when newly written."""
    dst = os.path.join(folder, "console_mods.cmake")
    if os.path.exists(dst):
        return None
    if not os.path.isfile(CMAKE_TEMPLATE):
        fail("templates/console_mods.cmake is missing (reinstall the console-mod skill)")
    with open(CMAKE_TEMPLATE, "r") as f:
        text = f.read()
    os.makedirs(folder, exist_ok=True)
    with open(dst, "w", newline="\n") as f:
        f.write(text)
    return ('wrote %s; add once at the end of ps2xRuntime/CMakeLists.txt: include("%s" OPTIONAL)'
            % (dst, dst.replace("\\", "/")))


def _pairs(values, flag):
    out = []
    for v in values or []:
        if "=" not in v:
            fail("%s needs 0xADDR=name, got %r" % (flag, v))
        a, _, n = v.partition("=")
        a, n = a.strip(), n.strip()
        try:
            addr = int(a, 16)
        except ValueError:
            fail("%s address is not hex: %r" % (flag, a))
        if not 0 <= addr <= 0xFFFFFFFF:
            fail("%s address out of range: %r" % (flag, a))
        if addr & 3:
            fail("%s address %s is not 4-byte aligned (functions start on a word)" % (flag, hex32(addr)))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n):
            fail("%s name %r is not a C++ identifier" % (flag, n))
        out.append((addr, n))
    return out


def build_new(a):
    elf = ElfKeys(a.elf)
    binds = _pairs(a.bind, "--bind")
    repl = _pairs(a.replace, "--replace")
    wraps = _pairs(a.wrap, "--wrap")
    if not (binds or repl or wraps):
        fail("give at least one --bind, --replace or --wrap (see --help)")
    warnings = []
    names = repo_handlers(a.repo) if a.repo else None
    resolved_binds = []
    for addr, h in binds:
        if names is not None:
            r = resolve_handler(h, names)
            if r is None:
                fail("--bind handler %r is not in PS2_SYSCALL_LIST or PS2_STUB_LIST of %s" % (h, a.repo))
            h = r
        elif h not in GENERIC_HANDLERS:
            warnings.append("handler %r not checked: pass --repo <PS2Recomp> to verify it exists" % h)
        resolved_binds.append((addr, h))
    seen_fn = set()
    for addr, fn in repl + wraps:
        if fn in CPP_KEYWORDS:
            fail("%r is a reserved name, pick another function name" % fn)
        if fn in seen_fn:
            fail("function name %r is used twice" % fn)
        seen_fn.add(fn)
    addrs = [x[0] for x in resolved_binds + repl + wraps]
    dup = sorted(set(x for x in addrs if addrs.count(x) > 1))
    if dup:
        fail("address used twice: %s" % ", ".join(hex32(x) for x in dup))
    starts = function_starts(a.map) if a.map else None
    reach_notes = []
    for addr in addrs:
        if elf.exec_ranges and not elf.in_code(addr):
            warnings.append("%s is not inside an executable segment of %s" % (hex32(addr), elf.name))
        if starts is not None and addr not in starts:
            warnings.append("%s is not a function start in the Ghidra map" % hex32(addr))
        sites = elf.call_sites(addr)
        nj, nt = len(sites["jal"]), len(sites["j"])
        reach_notes.append("  %s: reached by %d jal and %d j instruction(s) in the ELF" % (hex32(addr), nj, nt))
        if nt:
            warnings.append("%s is the target of %d j tail jump(s) (%s): PS2Recomp emits those as direct C++ calls "
                            "that skip the function table, so your function does not run on those paths%s"
                            % (hex32(addr), nt, ", ".join(hex32(x) for x in sites["j"][:4]),
                               "; nothing reaches it with jal: pick the caller one level up" if not nj else ""))

    fn = []
    apply = []
    for addr, h in resolved_binds:
        apply += [
            "    // --bind %s=%s: the game's function now runs the runtime's own %s handler." % (hex32(addr), h, h),
            "    // (Same effect as stubs = [\"%s@%s\"] in the TOML, without re-running ps2_recomp.)" % (h, hex32(addr)),
            "    ps2_game_overrides::bindAddressHandler(runtime, %su, \"%s\");" % (hex32(addr), h),
            "",
        ]
    for addr, name in repl:
        fn += [
            "// --replace %s=%s: runs INSTEAD of the game's function at %s." % (hex32(addr), name, hex32(addr)),
            "void %s(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)" % name,
            "{",
            "    (void)rdram;",
            "    (void)runtime;",
            "    // Arguments: read what the game's function reads (check it in Ghidra).",
            "    const uint32_t a0 = getRegU32(ctx, 4); // $a0, first argument",
            "    const uint32_t a1 = getRegU32(ctx, 5); // $a1, second argument",
            "    (void)a0;",
            "    (void)a1;",
            "",
            "    // TODO: the new behaviour. Guest memory: Ps2FastRead32(rdram, addr), Ps2FastWrite32(rdram, addr, value).",
            "",
            "    setReturnU32(ctx, 0u);        // the value the caller sees in $v0 (setReturnS32 for signed)",
            "    ctx->pc = getRegU32(ctx, 31); // return to the caller ($ra)",
            "}",
            "",
        ]
        apply += [
            "    // --replace %s=%s" % (hex32(addr), name),
            "    if (!runtime.replaceFunction(%su, &%s))" % (hex32(addr), name),
            "    {",
            "        std::cerr << \"[console-mod] %s: cannot replace %s (outside the recompiled function table)\" << std::endl;"
            % (c_string(a.name), hex32(addr)),
            "    }",
            "",
        ]
    for addr, name in wraps:
        orig = "g_original_" + name
        fn += [
            "PS2Runtime::RecompiledFunction %s = nullptr;" % orig,
            "",
            "// --wrap %s=%s: runs the game's own function, then lets you change what it did." % (hex32(addr), name),
            "void %s(uint8_t *rdram, R5900Context *ctx, PS2Runtime *runtime)" % name,
            "{",
            "    // Read arguments BEFORE the call: the game's code reuses these registers.",
            "    const uint32_t a0 = getRegU32(ctx, 4);",
            "    (void)a0;",
            "    const uint32_t returnAddress = getRegU32(ctx, 31);",
            "",
            "    %s(rdram, ctx, runtime); // the game's function: sets $v0 and ctx->pc" % orig,
            "",
            "    if (ctx->pc != returnAddress)",
            "    {",
            "        return; // it paused mid-way for the EE scheduler and finishes later without coming back here",
            "    }",
            "    // TODO: adjust the result. Example, double what the game returned:",
            "    // setReturnU32(ctx, getRegU32(ctx, 2) * 2u);",
            "}",
            "",
        ]
        apply += [
            "    // --wrap %s=%s" % (hex32(addr), name),
            "    if (runtime.hasFunction(%su))" % hex32(addr),
            "    {",
            "        PS2Runtime::RecompiledFunction current = runtime.lookupFunction(%su);" % hex32(addr),
            "        if (current != &%s)" % name,
            "        {",
            "            %s = current; // keep the game's function (safe if loadELF runs twice)" % orig,
            "        }",
            "        runtime.replaceFunction(%su, &%s);" % (hex32(addr), name),
            "    }",
            "    else",
            "    {",
            "        std::cerr << \"[console-mod] %s: no recompiled function at %s to wrap\" << std::endl;"
            % (c_string(a.name), hex32(addr)),
            "    }",
            "",
        ]
    while apply and apply[-1] == "":
        apply.pop()
    notes = [
        "Mod: %s" % a.name,
        "Generated by console-mod override.py new on %s for %s (%d bytes)." % (
            datetime.date.today().isoformat(), elf.name, elf.size),
    ]
    for addr, h in resolved_binds:
        notes.append("  bind    %s -> runtime handler %s" % (hex32(addr), h))
    for addr, name in repl:
        notes.append("  replace %s -> %s()" % (hex32(addr), name))
    for addr, name in wraps:
        notes.append("  wrap    %s -> %s() around the game's function" % (hex32(addr), name))
    notes.append("Replacing a function affects calls that go through the runtime's function table (jal,")
    notes.append("jalr and register jumps). A j tail jump to a known function is emitted as a direct C++")
    notes.append("call and skips it: if a target is reached only by j, hook the caller one level up.")
    notes += reach_notes
    slug = slugify(a.name)
    text = render_module(mod_name=a.name, elf_name=elf.name, entry=elf.entry, crc32=elf.crc32, slug=slug,
                         functions="\n".join(fn), apply_body="\n".join(apply), notes=notes)
    return elf, slug, text, warnings


def _guard_out(path):
    norm = os.path.abspath(path).replace("\\", "/")
    if "/ps2xRuntime/src/runner/" in norm + "/":
        fail("refusing to write into ps2xRuntime/src/runner/ (generated code): use console/patches/")
    if not norm.endswith(".cpp"):
        fail("the output must be a .cpp file: %s" % path)


def cmd_new(a):
    elf, slug, text, warnings = build_new(a)
    out = a.out or os.path.join("console", "patches", slug + ".cpp")
    _guard_out(out)
    if os.path.exists(out) and not a.force:
        fail("%s exists: pass --force to overwrite it (it may hold your own code)" % out)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", newline="\n") as f:
        f.write(text)
    cm = ensure_cmake_include(os.path.dirname(os.path.abspath(out)))
    print("wrote %s  (keys: %s, entry %s, CRC-32 %s)" % (out, elf.name, hex32(elf.entry), hex32(elf.crc32)))
    for w in warnings:
        print("  warning: " + w)
    print("  install: %s" % (cm if cm else "console_mods.cmake already next to it"))
    print("  (it adds the .cpp files to the ps2EntryRunner executable itself: inside the static ps2_runtime")
    print("  library the linker would drop the self-registration)")
    print("  then: cmake --build out/build --config Debug  (incremental, never clean)")
    return 1 if warnings else 0


def cmd_keys(a):
    elf = ElfKeys(a.elf)
    print('PS2_REGISTER_GAME_OVERRIDE("your mod", "%s", 0x%08Xu, 0x%08Xu, &applyOverride)' % (
        c_string(elf.name), elf.entry, elf.crc32))
    print("  file name %s, entry %s, CRC-32 %s (standard CRC-32 of the whole ELF, not the PCSX2 CRC)"
          % (elf.name, hex32(elf.entry), hex32(elf.crc32)))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="override.py",
        description="Scaffold a PS2Recomp game override (.cpp) with the real API: bind game functions to "
                    "runtime handlers, replace them, or wrap them.",
        epilog="Exit codes: 0 ok, 1 written with warnings, 2 bad input.")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("new", help="write a new override module")
    sp.add_argument("--name", required=True, help='the mod name, e.g. "Gold rifle" (also names the file)')
    sp.add_argument("--elf", required=True, help="the game's main ELF: gives the file name, entry and CRC-32 keys")
    sp.add_argument("--bind", action="append", metavar="0xADDR=handler",
                    help="route the game function at ADDR to a runtime handler (repeatable)")
    sp.add_argument("--replace", action="append", metavar="0xADDR=fn_name",
                    help="replace the game function at ADDR with a C++ function you fill in (repeatable)")
    sp.add_argument("--wrap", action="append", metavar="0xADDR=fn_name",
                    help="run the game function at ADDR, then your code (repeatable)")
    sp.add_argument("--map", help="Ghidra CSV (Name,Start,End,Size) to check addresses are function starts")
    sp.add_argument("--repo", help="PS2Recomp checkout, to check --bind handler names against ps2_call_list.h")
    sp.add_argument("--out", help="output .cpp (default console/patches/<slug>.cpp)")
    sp.add_argument("--force", action="store_true", help="overwrite the output if it exists")
    sp.set_defaults(func=cmd_new)
    sp = sub.add_parser("keys", help="print the override keys (file name, entry, CRC-32) for an ELF")
    sp.add_argument("elf", help="the game's main ELF")
    sp.set_defaults(func=cmd_keys)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        p.print_help()
        return 2
    try:
        return a.func(a)
    except UsageError as e:
        print("%s: error: %s" % (TOOL, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
