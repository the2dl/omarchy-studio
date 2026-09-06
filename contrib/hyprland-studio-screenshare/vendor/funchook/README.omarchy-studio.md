# Vendored funchook (arm64 only)

Upstream: <https://github.com/kubo/funchook> @ `b499170`. Licence: GPL-2.0-or-later
**with a linking exception** (see `LICENSE`) — the exception is what makes it usable
from this plugin; read it before changing how it is linked.

## Why this is here

Hyprland's own `CFunctionHook` is x86-64 only. `CFunctionHook::hook()` opens with

```c
// check for unsupported platforms
#if !defined(__x86_64__)
    return false;
#endif
```

(`src/plugins/HookSystem.cpp`, identical in v0.56.1 and main) because the mechanism is
an inline trampoline assembled from raw x86 opcodes — `0xE9` rel32 jmp, `movabs`+`jmp
*%rax`, `0x90` padding — with the displaced prologue decoded by the udis86 *x86*
disassembler. None of it ports: aarch64 has fixed 4-byte instructions, no absolute
branch (`B` reaches only ±128 MB), a pile of PC-relative forms that need re-encoding
when moved (`ADR`, `ADRP`, `B`, `BL`, `CBZ/CBNZ`, `TBZ/TBNZ`, literal `LDR`), and a
non-coherent i-cache that must be flushed after patching.

Upstream is not going to fix it: hyprwm/Hyprland#15684 ("Add function hook support for
ARM64") was opened by the maintainer himself, is `low prio` with no one on it, and
hyprland-plugins#438 was closed `not_planned`.

funchook does all of the above on arm64 already, so the six hooks in `src/main.cpp` go
through it on aarch64 and through the stock `HyprlandAPI` path everywhere else. x86-64
behaviour is unchanged — there is no reason to move a working platform onto a vendored
library.

## What was taken

Only the arm64 + unix + capstone subset, five translation units:

    src/funchook.c  src/arch_arm64.c  src/disasm_capstone.c  src/os_unix.c
    src/prehook-arm64-gas.S

plus their headers, `include/funchook.h`, and `src/config.h` — which is written by hand
here instead of generated, so the build needs no network and no funchook CMake.

Everything x86, Windows, distorm and Zydis was left out. capstone comes from the system
(`pkg-config capstone`), not FetchContent.

## Updating it

Re-copy those files from a newer funchook tag and re-run the plugin's own test rig.
`config.h` is ours; do not overwrite it with the generated one.
