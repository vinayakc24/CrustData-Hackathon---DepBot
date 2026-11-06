cmake_minimum_required(VERSION 3.20)
project(s02 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s02 src/main.cpp)
target_link_libraries(s02 PRIVATE toydep::toydep)
add_test(NAME s02 COMMAND s02)
