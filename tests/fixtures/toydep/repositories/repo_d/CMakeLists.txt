cmake_minimum_required(VERSION 3.20)
project(toy_repo_d LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(repo_d_test src/main.cpp)
target_link_libraries(repo_d_test PRIVATE toydep::toydep)
add_test(NAME repo_d_test COMMAND repo_d_test)
