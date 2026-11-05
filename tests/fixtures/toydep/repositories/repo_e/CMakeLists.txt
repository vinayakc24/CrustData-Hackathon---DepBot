cmake_minimum_required(VERSION 3.20)
project(toy_repo_e LANGUAGES CXX)

enable_testing()
find_package(toydep REQUIRED)

add_executable(repo_e_test src/main.cpp)
target_link_libraries(repo_e_test PRIVATE toydep::toydep)
add_test(NAME repo_e_test COMMAND repo_e_test)
