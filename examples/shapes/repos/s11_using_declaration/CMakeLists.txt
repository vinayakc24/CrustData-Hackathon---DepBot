cmake_minimum_required(VERSION 3.20)
project(s11 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s11 src/main.cpp)
target_link_libraries(s11 PRIVATE toydep::toydep)
add_test(NAME s11 COMMAND s11)
