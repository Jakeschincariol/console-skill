#!/usr/bin/env python3
"""abdiff.py: compare PCSX2 (A, the original game) against the port (B) (console-tester).

`regs A B` compares two register dumps: JSON (name -> value, nested groups are
flattened) or plain text with name=value pairs (like the runtime's own trace lines).
`mem A.bin B.bin --base 0x..` compares two binary memory dumps and prints the first
divergence and the differing ranges. Exit 0 when they match, 1 when they differ.

Python 3.8+ standard library only.
"""

import argparse
import json
import re
import struct
import sys

GPR_NAMES = ["zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5",
             "t6", "t7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9", "k0", "k1",
             "gp", "sp", "fp", "ra"]
ORDER = ["pc"] + GPR_NAMES + ["hi", "lo", "hi1", "lo1", "sa"]
CHUNK = 1 << 20


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- registers

def canonical(name):
    n = str(name).strip().lower().lstrip("$")
    m = re.fullmatch(r"(?:r|gpr)\[?(\d{1,2})\]?", n)
    if m and int(m.group(1)) < 32:
        return GPR_NAMES[int(m.group(1))]
    if n == "s8":
        return "fp"
    if n == "r0":
        return "zero"
    return n


def parse_value(value, where):
    if isinstance(value, bool):
        raise UsageError("%s: true/false is not a register value" % where)
    if isinstance(value, int):
        if value < 0:
            value &= (1 << 128) - 1
        return value
    if isinstance(value, float):
        return struct.unpack("<I", struct.pack("<f", value))[0]
    if isinstance(value, list):
        total = 0
        for i, word in enumerate(value):
            total |= (parse_value(word, where) & 0xFFFFFFFF) << (32 * i)
        return total
    if isinstance(value, str):
        text = value.strip().replace("_", "").replace(" ", "")
        if text.lower().startswith("0x"):
            text = text[2:]
        if not text or not re.fullmatch(r"[0-9a-fA-F]+", text):
            raise UsageError("%s: %r is not a hex value" % (where, value))
        return int(text, 16)
    raise UsageError("%s: unsupported value %r" % (where, value))


def _flatten(obj, out, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict):
                _flatten(value, out, "%s%s." % (prefix, key))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                _flatten(value, out, prefix)
            else:
                out.append((str(key), prefix + str(key), value))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and "name" in item and "value" in item:
                out.append((str(item["name"]), prefix + str(item["name"]), item["value"]))
            elif isinstance(item, dict):
                _flatten(item, out, prefix)


TEXT_PAIR = re.compile(r"(?<![\w\[])\$?([A-Za-z][A-Za-z0-9]*)\s*[=:]\s*(0x[0-9a-fA-F_]+|[0-9a-fA-F]{8,32}\b|\d+\b)")


def load_regs(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise UsageError("no such file: %s" % path)
    except OSError as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))
    regs = {}
    stripped = text.strip()
    data = None
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if stripped.startswith("{"):
                raise UsageError("%s looks like JSON but is not valid (line %d: %s)"
                                 % (path, exc.lineno, exc.msg))
            data = None  # a log line like "[guest-branch:...] pc=0x..." is text
    if data is not None:
        items = []
        _flatten(data, items)
        short_counts = {}
        for short, _, _ in items:
            short_counts[canonical(short)] = short_counts.get(canonical(short), 0) + 1
        for short, full, value in items:
            name = canonical(short) if short_counts[canonical(short)] == 1 else full.lower()
            regs[name] = parse_value(value, "%s: %s" % (path, full))
    else:
        for m in TEXT_PAIR.finditer(text):
            raw = m.group(2)
            value = int(raw, 10) if raw.isdigit() and not raw.lower().startswith("0x") and len(raw) < 8 \
                else parse_value(raw, "%s: %s" % (path, m.group(1)))
            regs.setdefault(canonical(m.group(1)), value)
    if not regs:
        raise UsageError("%s has no registers (JSON name -> value, or name=0x... text)" % path)
    return regs


def sort_names(names):
    rank = {n: i for i, n in enumerate(ORDER)}
    return sorted(names, key=lambda n: (rank.get(n, len(ORDER)), n))


def diff_regs(a, b, width=128, only=None, ignore=()):
    mask = (1 << width) - 1
    names_a, names_b = set(a), set(b)
    common = names_a & names_b
    if only:
        common &= set(only)
    common -= set(ignore)
    digits = max(1, width // 4)
    diffs = []
    for name in sort_names(common):
        va, vb = a[name] & mask, b[name] & mask
        if va != vb:
            diffs.append({"name": name, "a": "0x%0*x" % (digits, va), "b": "0x%0*x" % (digits, vb),
                          "xor": "0x%0*x" % (digits, va ^ vb)})
    return {
        "compared": len(common), "width": width, "differences": diffs,
        "first": diffs[0] if diffs else None,
        "only_in_a": sort_names(names_a - names_b - set(ignore)),
        "only_in_b": sort_names(names_b - names_a - set(ignore)),
    }


def cmd_regs(args):
    if args.width not in (32, 64, 128):
        raise UsageError("--width must be 32, 64 or 128")
    a, b = load_regs(args.a), load_regs(args.b)
    only = [canonical(n) for n in args.only.split(",")] if args.only else None
    ignore = [canonical(n) for n in args.ignore.split(",")] if args.ignore else []
    result = diff_regs(a, b, args.width, only, ignore)
    if result["compared"] == 0:
        raise UsageError("no register appears in both dumps%s" % (" (after --only/--ignore)"
                                                                   if (only or ignore) else ""))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("registers: %d compared at %d bits (A = %s, B = %s)" % (result["compared"], args.width,
                                                                      args.a, args.b))
        if not result["differences"]:
            print("  identical")
        else:
            first = result["first"]
            print("  first divergence: %s  A=%s  B=%s" % (first["name"], first["a"], first["b"]))
            if first["name"] == "pc":
                print("  pc differs: the dumps are not from the same point. Break at the same address "
                      "on both sides first.")
            for d in result["differences"]:
                print("  %-6s A=%s  B=%s  xor=%s" % (d["name"], d["a"], d["b"], d["xor"]))
        if result["only_in_a"]:
            print("  only in A: %s" % ", ".join(result["only_in_a"]))
        if result["only_in_b"]:
            print("  only in B: %s" % ", ".join(result["only_in_b"]))
    return 1 if result["differences"] else 0


# ---------------------------------------------------------------- memory

def parse_int(text, what):
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        raise UsageError("%s %r is not a number (use hex like 0x00100000)" % (what, text))


def parse_range(text):
    m = re.fullmatch(r"\s*(0x[0-9a-fA-F]+|\d+)\s*[-:]\s*(0x[0-9a-fA-F]+|\d+)\s*", text)
    if not m:
        raise UsageError("--ignore-range %r should look like 0x00200000-0x00200100" % text)
    start, end = int(m.group(1), 0), int(m.group(2), 0)
    if end <= start:
        raise UsageError("--ignore-range %r ends before it starts" % text)
    return start, end


def _open(path):
    try:
        return open(path, "rb")
    except FileNotFoundError:
        raise UsageError("no such file: %s" % path)
    except IsADirectoryError:
        raise UsageError("%s is a folder, not a memory dump" % path)
    except OSError as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))


def diff_mem(path_a, path_b, base=0, gap=16, max_ranges=50, ignore=(), context=16):
    fa, fb = _open(path_a), _open(path_b)
    ranges = []
    total_ranges = 0
    differing = 0
    current = None
    offset = 0
    size_a = size_b = 0

    def ignored(off):
        addr = base + off
        return any(s <= addr < e for s, e in ignore)

    def close_current():
        nonlocal current, total_ranges
        if current is not None:
            total_ranges += 1
            if len(ranges) < max_ranges:
                ranges.append(current)
            current = None

    def mark(off):
        nonlocal current, differing
        if ignore and ignored(off):
            return
        differing += 1
        if current is not None and off - current[1] <= gap:
            current[1] = off + 1
        else:
            close_current()
            current = [off, off + 1]

    first = None
    try:
        while True:
            ca, cb = fa.read(CHUNK), fb.read(CHUNK)
            size_a += len(ca)
            size_b += len(cb)
            if not ca and not cb:
                break
            n = min(len(ca), len(cb))
            if ca[:n] != cb[:n]:
                i = 0
                while i < n:
                    # skip equal 64-byte blocks quickly
                    if ca[i:i + 64] == cb[i:i + 64]:
                        i += 64
                        continue
                    end = min(i + 64, n)
                    for j in range(i, end):
                        if ca[j] != cb[j]:
                            if first is None and not (ignore and ignored(offset + j)):
                                first = offset + j
                            mark(offset + j)
                    i = end
            longer = max(len(ca), len(cb))
            if longer > n and not ignore:
                # one file is longer: its tail is one difference, marked in one step
                if first is None:
                    first = offset + n
                differing += longer - n
                if current is not None and offset + n - current[1] <= gap:
                    current[1] = offset + longer
                else:
                    close_current()
                    current = [offset + n, offset + longer]
            else:
                for j in range(n, longer):
                    if first is None and not ignored(offset + j):
                        first = offset + j
                    mark(offset + j)
            offset += longer
        close_current()

        detail = None
        if first is not None:
            start = max(0, first - context)
            fa.seek(start)
            fb.seek(start)
            blob_a, blob_b = fa.read(2 * context), fb.read(2 * context)
            word_off = first & ~3
            fa.seek(word_off)
            fb.seek(word_off)
            wa, wb = fa.read(4), fb.read(4)
            detail = {
                "address": "0x%08x" % (base + first), "offset": first,
                "a_byte": "0x%02x" % blob_a[first - start] if first - start < len(blob_a) else None,
                "b_byte": "0x%02x" % blob_b[first - start] if first - start < len(blob_b) else None,
                "a_word": "0x%08x" % struct.unpack("<I", wa)[0] if len(wa) == 4 else None,
                "b_word": "0x%08x" % struct.unpack("<I", wb)[0] if len(wb) == 4 else None,
                "word_address": "0x%08x" % (base + word_off),
                "context_address": "0x%08x" % (base + start),
                "a_context": blob_a.hex(), "b_context": blob_b.hex(),
            }
    finally:
        fa.close()
        fb.close()
    return {
        "a": path_a, "b": path_b, "base": "0x%08x" % base, "size_a": size_a, "size_b": size_b,
        "differing_bytes": differing, "range_count": total_ranges,
        "ranges": [{"start": "0x%08x" % (base + s), "end": "0x%08x" % (base + e), "length": e - s}
                   for s, e in ranges],
        "truncated": total_ranges > len(ranges), "first": detail,
    }


def _hexdump_line(hexstr, address):
    data = bytes.fromhex(hexstr)
    return "0x%08x  %s" % (address, " ".join("%02x" % b for b in data))


def cmd_mem(args):
    base = parse_int(args.base, "--base")
    if base < 0:
        raise UsageError("--base must not be negative")
    if args.gap < 0 or args.max_ranges < 1:
        raise UsageError("--gap must be 0 or more and --max-ranges at least 1")
    ignore = [parse_range(r) for r in args.ignore_range]
    result = diff_mem(args.a, args.b, base, args.gap, args.max_ranges, ignore)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("memory: A %s (%d bytes), B %s (%d bytes), base %s" % (args.a, result["size_a"], args.b,
                                                                      result["size_b"], result["base"]))
        if result["size_a"] != result["size_b"]:
            print("  sizes differ: the extra tail counts as different. Dump the same range on both sides.")
        first = result["first"]
        if first is None:
            print("  identical" + (" (outside the ignored ranges)" if ignore else ""))
        else:
            print("  first divergence at %s: A=%s B=%s (word at %s: A=%s B=%s)" % (
                first["address"], first["a_byte"], first["b_byte"], first["word_address"],
                first["a_word"], first["b_word"]))
            ctx = int(first["context_address"], 16)
            print("  A " + _hexdump_line(first["a_context"], ctx))
            print("  B " + _hexdump_line(first["b_context"], ctx))
            print("  %d differing byte(s) in %d range(s)%s:" % (
                result["differing_bytes"], result["range_count"],
                " (first %d shown)" % len(result["ranges"]) if result["truncated"] else ""))
            for r in result["ranges"]:
                print("    %s - %s  %d bytes" % (r["start"], r["end"], r["length"]))
    return 1 if result["first"] is not None else 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="abdiff.py",
        description=("A/B compare PCSX2 (A, the original running) against the port (B): register dumps "
                     "or binary memory dumps. The first divergence is where to look."))
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sp = sub.add_parser("regs", help="compare two register dumps (JSON or name=value text)")
    sp.add_argument("a", help="side A: PCSX2 (ground truth)")
    sp.add_argument("b", help="side B: the port")
    sp.add_argument("--width", type=int, default=128,
                    help="compare only the low 32, 64 or 128 bits (default 128; use 32 when the port logs u32)")
    sp.add_argument("--only", help="comma list of registers to compare, like v0,a0,sp,ra")
    sp.add_argument("--ignore", help="comma list of registers to skip, like k0,k1")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_regs)

    sp = sub.add_parser("mem", help="compare two binary memory dumps")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("--base", default="0", help="guest address of the first byte (like 0x00100000)")
    sp.add_argument("--gap", type=int, default=16, help="merge differences closer than this many bytes")
    sp.add_argument("--max-ranges", type=int, default=50)
    sp.add_argument("--ignore-range", action="append", default=[], metavar="START-END",
                    help="guest address range to ignore (timers, counters); repeatable")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_mem)
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
