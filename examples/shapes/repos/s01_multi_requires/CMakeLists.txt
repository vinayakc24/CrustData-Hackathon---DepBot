cmake_minimum_required(VERSION 3.20)
project(s01 LANGUAGES CXX)

enable_testing()
find_package(otherdep REQUIRED)
find_package(toydep REQUIRED)

add_executable(s01 src/main.cpp)
target_link_libraries(s01 PRIVATE otherdep::otherdep toydep::toydep)
add_test(NAME s01 COMMAND s01)
