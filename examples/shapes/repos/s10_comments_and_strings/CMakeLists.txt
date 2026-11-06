cmake_minimum_required(VERSION 3.20)
project(s10 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s10 src/main.cpp)
target_link_libraries(s10 PRIVATE toydep::toydep)
add_test(NAME s10 COMMAND s10)
