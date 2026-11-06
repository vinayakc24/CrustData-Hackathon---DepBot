cmake_minimum_required(VERSION 3.20)
project(s06 LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(s06 src/main.cpp)
target_link_libraries(s06 PRIVATE toydep::toydep)
