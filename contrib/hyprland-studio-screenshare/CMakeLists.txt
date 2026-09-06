cmake_minimum_required(VERSION 3.27)
project(omarchy-studio-screenshare
        DESCRIPTION "Exclude no_screen_share windows from screenshares instead of blacking them out"
        VERSION 1.0
        LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 26)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Hyprland itself is built -fno-lto "as it may break plugins" (its CMakeLists.txt);
# a plugin built WITH lto against it is the same hazard from the other side.
add_compile_options(-fno-lto -fPIC -Wall -Wextra -Wno-unused-parameter)

find_package(PkgConfig REQUIRED)
pkg_check_modules(deps REQUIRED IMPORTED_TARGET
                  hyprland hyprutils hyprlang hyprgraphics aquamarine
                  libdrm egl glesv2 cairo pango pangocairo pixman-1
                  wayland-server xkbcommon libinput)

add_library(omarchy-studio-screenshare SHARED src/main.cpp)
target_link_libraries(omarchy-studio-screenshare PRIVATE PkgConfig::deps)
set_target_properties(omarchy-studio-screenshare PROPERTIES PREFIX "")

# Hyprland's CFunctionHook is x86-64 only -- CFunctionHook::hook() opens with
# `#if !defined(__x86_64__) return false;` because it assembles a raw x86 trampoline.
# On aarch64 the same six seams are hooked with vendored funchook instead, which emits
# an AArch64 trampoline, relocates the displaced PC-relative instructions and flushes
# the i-cache. See vendor/funchook/README.omarchy-studio.md.
if (CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
    # Enabled HERE rather than in project(), so an x86-64 build needs neither a C
    # compiler nor an assembler it would never use. Only the vendored funchook wants
    # them, and only this branch builds it.
    enable_language(C)
    enable_language(ASM)

    pkg_check_modules(capstone REQUIRED IMPORTED_TARGET capstone)

    add_library(funchook_arm64 STATIC
                vendor/funchook/src/funchook.c
                vendor/funchook/src/arch_arm64.c
                vendor/funchook/src/disasm_capstone.c
                vendor/funchook/src/os_unix.c
                vendor/funchook/src/prehook-arm64-gas.S)
    target_include_directories(funchook_arm64
                               PUBLIC  vendor/funchook/include
                               PRIVATE vendor/funchook/src)
    target_link_libraries(funchook_arm64 PRIVATE PkgConfig::capstone)
    set_target_properties(funchook_arm64 PROPERTIES POSITION_INDEPENDENT_CODE ON)
    # Vendored third-party code: its warnings are not ours to fix, and they would bury
    # any real warning from src/main.cpp.
    target_compile_options(funchook_arm64 PRIVATE -w)

    target_link_libraries(omarchy-studio-screenshare PRIVATE funchook_arm64)
    target_compile_definitions(omarchy-studio-screenshare PRIVATE OMARCHY_STUDIO_FUNCHOOK=1)
endif ()

install(TARGETS omarchy-studio-screenshare LIBRARY DESTINATION lib)
