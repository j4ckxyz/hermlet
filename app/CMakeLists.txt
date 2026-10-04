cmake_minimum_required(VERSION 3.16)
project(hermlet_app C)

# The Hermlet app: a small native window onto your Hermes agent.
#
#   cmake -S app -B app/build -DCMAKE_BUILD_TYPE=Release && cmake --build app/build -j
#
# Backends (pick with -DHERMLET_BACKEND=...):
#   sdl    (default) a window on any desktop; on a Raspberry Pi without a desktop SDL
#          draws straight to the screen through KMS/DRM, so one build covers both.
#   fbdev  the Linux framebuffer and evdev input, with no SDL at all. For tiny
#          SPI screens and minimal systems. Linux only.
#   offscreen  no display and no libraries beyond libc: draws into memory. For machines
#          with no screen (tests, CI, pictures with --screenshot).

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(HERMLET_BACKEND "sdl" CACHE STRING "Display backend: sdl, fbdev or offscreen")
set_property(CACHE HERMLET_BACKEND PROPERTY STRINGS sdl fbdev offscreen)

include(FetchContent)
set(FETCHCONTENT_QUIET OFF)

# LVGL reads its settings from app/lv_conf.h.
set(LV_CONF_BUILD_DISABLE_EXAMPLES ON CACHE BOOL "" FORCE)
set(LV_CONF_BUILD_DISABLE_DEMOS ON CACHE BOOL "" FORCE)
set(LV_CONF_BUILD_DISABLE_THORVG_INTERNAL ON CACHE BOOL "" FORCE)
set(LV_CONF_PATH "${CMAKE_CURRENT_SOURCE_DIR}/lv_conf.h" CACHE STRING "" FORCE)
FetchContent_Declare(lvgl
  GIT_REPOSITORY https://github.com/lvgl/lvgl.git
  GIT_TAG v9.2.2
  GIT_SHALLOW TRUE)

# cJSON is two files; only the source is fetched and it is compiled as part of the app
# (its own build script is older than current CMake accepts).
FetchContent_Declare(cjson
  GIT_REPOSITORY https://github.com/DaveGamble/cJSON.git
  GIT_TAG v1.7.18
  GIT_SHALLOW TRUE
  SOURCE_SUBDIR not-built-as-a-project)

if(HERMLET_BACKEND STREQUAL "sdl")
  find_package(SDL2 REQUIRED)
  add_compile_definitions(HERMLET_SDL=1)
elseif(HERMLET_BACKEND STREQUAL "fbdev")
  add_compile_definitions(HERMLET_FBDEV=1)
elseif(HERMLET_BACKEND STREQUAL "offscreen")
  add_compile_definitions(HERMLET_OFFSCREEN=1)
else()
  message(FATAL_ERROR "HERMLET_BACKEND must be sdl, fbdev or offscreen")
endif()

FetchContent_MakeAvailable(lvgl cjson)

add_executable(hermlet-app
  src/main.c
  src/ipc.c
  src/critter.c
  src/ui.c
  ${cjson_SOURCE_DIR}/cJSON.c)
target_include_directories(hermlet-app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR} ${cjson_SOURCE_DIR})
target_compile_options(hermlet-app PRIVATE -Wall -Wextra -Wno-unused-parameter)
if(CMAKE_C_COMPILER_ID STREQUAL "GNU")
  # Text is cut to fit fixed fields on purpose (copy() in ui.c).
  target_compile_options(hermlet-app PRIVATE -Wno-format-truncation)
endif()
target_link_libraries(hermlet-app PRIVATE lvgl m)

if(HERMLET_BACKEND STREQUAL "sdl")
  if(TARGET SDL2::SDL2)
    target_link_libraries(lvgl PUBLIC SDL2::SDL2)
  else()
    target_include_directories(lvgl PUBLIC ${SDL2_INCLUDE_DIRS})
    target_link_libraries(lvgl PUBLIC ${SDL2_LIBRARIES})
  endif()
endif()

install(TARGETS hermlet-app RUNTIME DESTINATION bin)
