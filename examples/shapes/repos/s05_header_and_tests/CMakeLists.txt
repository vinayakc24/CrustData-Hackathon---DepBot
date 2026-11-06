cmake_minimum_required(VERSION 3.20)
project(s05 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s05 tests/test_main.cpp)
target_include_directories(s05 PRIVATE include)
target_link_libraries(s05 PRIVATE toydep::toydep)
add_test(NAME s05 COMMAND s05)
