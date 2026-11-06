cmake_minimum_required(VERSION 3.20)
project(s09 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s09 src/main.cpp)
target_link_libraries(s09 PRIVATE toydep::toydep)
add_test(NAME s09 COMMAND s09)
