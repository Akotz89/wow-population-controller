cmake_minimum_required(VERSION 3.20)
# Refuse the wrong host before project() probes any compiler.
execute_process(COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/../scripts/wow-build-host-guard"
    RESULT_VARIABLE PBGUIDE_GUARD_RESULT)
if(NOT PBGUIDE_GUARD_RESULT STREQUAL "0")
    message(FATAL_ERROR "WoW build-host guard refused")
endif()
project(playerbots_strategic_director LANGUAGES CXX)
if(POLICY CMP0167)
    cmake_policy(SET CMP0167 NEW)
endif()
find_package(Boost 1.81 REQUIRED COMPONENTS json)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(Python3 3.14 EXACT REQUIRED COMPONENTS Interpreter Development.Module)
if(NOT Python3_SOABI STREQUAL "cpython-314-x86_64-linux-gnu")
    message(FATAL_ERROR "Expected accepted CPython SOABI cpython-314-x86_64-linux-gnu")
endif()
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
add_library(playerbots_strategic_director STATIC
    src/activity_reservations.cpp
    src/director.cpp
    src/failure_memory.cpp
    src/canonical_item_sources.cpp
    src/gear_planner.cpp
    src/route_graph.cpp)
target_include_directories(playerbots_strategic_director PUBLIC include PRIVATE src)
target_link_libraries(playerbots_strategic_director PUBLIC Boost::json OpenSSL::Crypto)
target_compile_options(playerbots_strategic_director PRIVATE
    -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)
add_library(playerbots_strategic_replay STATIC
    src/gear_plan_json.cpp
    src/gear_plan_store.cpp
    src/replay.cpp)
target_include_directories(playerbots_strategic_replay PUBLIC include PRIVATE src)
target_link_libraries(playerbots_strategic_replay PUBLIC playerbots_strategic_director Boost::json)
target_compile_options(playerbots_strategic_replay PRIVATE
    -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)
Python3_add_library(_pbguide_gear MODULE WITH_SOABI src/python_gear.cpp)
target_include_directories(_pbguide_gear PRIVATE src)
target_link_libraries(_pbguide_gear PRIVATE playerbots_strategic_replay)
# Guard retained build trees again before every owned target can compile.
add_custom_target(pbguide_build_host_precheck
    COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/../scripts/wow-build-host-guard" VERBATIM)
foreach(target playerbots_strategic_director playerbots_strategic_replay _pbguide_gear)
    add_dependencies(${target} pbguide_build_host_precheck)
endforeach()
