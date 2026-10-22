// @MOD_NAME@: a PS2Recomp game override made with the Console skill (console-mod).
//
// This is the corrected override template. It uses the API that exists in
// PS2Recomp today (ps2xRuntime/include/game_overrides.h and ps2_runtime.h),
// not the Runtime.h / Runtime::OverrideFunction API the original agent skill
// shipped, which never existed. override.py and pnach.py fill in the fields
// written between @ signs; to write one by hand, replace each of them yourself.
//
// How it runs: ps2EntryRunner calls applyOverride() inside PS2Runtime::loadELF,
// after the ELF's PT_LOAD segments are in RDRAM and before the game's first
// instruction, and only when the booted ELF matches all three keys at the
// bottom (file name, entry point, CRC-32 of the whole ELF file).
//
// Install once per PS2Recomp checkout: add this line at the END of
// ps2xRuntime/CMakeLists.txt (console_mods.cmake sits next to this file):
//   include("/full/path/to/console/patches/console_mods.cmake" OPTIONAL)
// That adds this file to the ps2EntryRunner executable itself: compiled into the
// static ps2_runtime library, a file holding only a registration is dropped by
// the linker and never runs. Then rebuild incrementally:
//   cmake --build out/build --config Debug   (never clean the build)
//
// Replaced and wrapped functions only see calls that go through the runtime's
// function table (jal, jalr). A j tail jump to a known function is compiled to
// a direct C++ call and skips it: hook the caller one level up in that case.
//
// Share this file. Never share the game's files or the generated runner/*.cpp.
//
@NOTES@
#include "game_overrides.h"
#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"

#include <cstdint>
#include <iostream>

// Guest calling convention (R5900, EE ABI) for replacement functions:
//   arguments  $a0..$a3 = getRegU32(ctx, 4..7), more on the stack ($sp = 29)
//   floats     ctx->f[12], ctx->f[13], ... in (EABI: up to f[19]), ctx->f[0] out
//   return     setReturnU32(ctx, v) / setReturnS32(ctx, v) puts v in $v0
//   go back    ctx->pc = getRegU32(ctx, 31);   ($ra, the caller's return address)
//   memory     Ps2FastRead32(rdram, addr), Ps2FastWrite32(rdram, addr, v)
//              (8/16/64-bit versions too; addresses are masked to 32 MB RDRAM)

namespace @NAMESPACE@
{
@FUNCTIONS@
void applyOverride(PS2Runtime &runtime)
{
    uint8_t *rdram = runtime.memory().getRDRAM();
    (void)rdram;
@APPLY_BODY@
}

// Keys: mod name (for the log), ELF file name (matched case-insensitively),
// entry point, CRC-32 of the ELF file. A CRC of 0 matches any build: never
// ship that, addresses differ between regions and versions.
PS2_REGISTER_GAME_OVERRIDE("@MOD_NAME@", "@ELF_NAME@", @ENTRY@, @CRC32@, &applyOverride)
} // namespace @NAMESPACE@
