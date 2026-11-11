cmake_minimum_required(VERSION 3.20)
project(behavior_change LANGUAGES CXX)

enable_testing()
find_package(scaledep REQUIRED)

add_executable(behavior_change_test src/main.cpp)
target_link_libraries(behavior_change_test PRIVATE scaledep::scaledep)
add_test(NAME behavior_change_test COMMAND behavior_change_test)
