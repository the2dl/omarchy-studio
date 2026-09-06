/* Hand-written stand-in for the header funchook's own CMake generates from
   src/cmake_config.h.in. Vendored here so the plugin builds with nothing but a
   compiler and system capstone -- funchook's CMakeLists unconditionally
   FetchContent's its own capstone, which would put a network fetch in the middle
   of every `hyprpm update`. */
#define _GNU_SOURCE
#define GNU_SPECIFIC_STRERROR_R 1
#define DISASM_CAPSTONE 1
#define SIZEOF_VOID_P 8
