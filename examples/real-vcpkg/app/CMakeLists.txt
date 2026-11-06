cmake_minimum_required(VERSION 3.20)
project(toydep_vcpkg_service LANGUAGES CXX)

enable_testing()
find_package(toydep CONFIG REQUIRED)

add_executable(service_test src/main.cpp)
target_link_libraries(service_test PRIVATE toydep::toydep)
add_test(NAME service_test COMMAND service_test)
