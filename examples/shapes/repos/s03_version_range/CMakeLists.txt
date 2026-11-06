cmake_minimum_required(VERSION 3.20)
project(s03 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s03 src/main.cpp)
target_link_libraries(s03 PRIVATE toydep::toydep)
add_test(NAME s03 COMMAND s03)
