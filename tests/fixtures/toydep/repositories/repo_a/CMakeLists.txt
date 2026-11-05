cmake_minimum_required(VERSION 3.20)
project(toy_repo_a LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(repo_a_test src/main.cpp)
target_link_libraries(repo_a_test PRIVATE toydep::toydep)
add_test(NAME repo_a_test COMMAND repo_a_test)
