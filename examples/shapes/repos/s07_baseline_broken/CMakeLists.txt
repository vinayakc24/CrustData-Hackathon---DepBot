cmake_minimum_required(VERSION 3.20)
project(s07 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s07 src/main.cpp)
target_link_libraries(s07 PRIVATE toydep::toydep)
add_test(NAME s07 COMMAND s07)
