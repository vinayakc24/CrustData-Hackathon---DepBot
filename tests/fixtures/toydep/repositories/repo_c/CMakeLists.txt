cmake_minimum_required(VERSION 3.20)
project(toy_repo_c LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(repo_c_test src/main.cpp)
target_link_libraries(repo_c_test PRIVATE toydep::toydep)
add_test(NAME repo_c_test COMMAND repo_c_test)
