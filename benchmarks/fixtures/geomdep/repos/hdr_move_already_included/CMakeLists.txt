cmake_minimum_required(VERSION 3.20)
project(hdr_move_dup LANGUAGES CXX)

enable_testing()
find_package(geomdep REQUIRED)

add_executable(hdr_move_dup_test src/main.cpp)
target_link_libraries(hdr_move_dup_test PRIVATE geomdep::geomdep)
add_test(NAME hdr_move_dup_test COMMAND hdr_move_dup_test)
