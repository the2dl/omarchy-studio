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

install(TARGETS omarchy-studio-screenshare LIBRARY DESTINATION lib)
