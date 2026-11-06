cmake_minimum_required(VERSION 3.20)
project(s08 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s08 src/main.cpp)
target_link_libraries(s08 PRIVATE toydep::toydep)
add_test(NAME s08 COMMAND s08)
