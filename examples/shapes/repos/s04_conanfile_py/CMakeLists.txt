cmake_minimum_required(VERSION 3.20)
project(s04 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s04 src/main.cpp)
target_link_libraries(s04 PRIVATE toydep::toydep)
add_test(NAME s04 COMMAND s04)
