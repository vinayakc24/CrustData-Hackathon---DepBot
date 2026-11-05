cmake_minimum_required(VERSION 3.20)
project(fmt_point_service LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

enable_testing()
find_package(fmt REQUIRED)

add_executable(point_test src/main.cpp)
target_link_libraries(point_test PRIVATE fmt::fmt)
add_test(NAME point_test COMMAND point_test)
