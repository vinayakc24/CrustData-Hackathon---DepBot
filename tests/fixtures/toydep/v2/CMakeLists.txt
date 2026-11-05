add_library(toydep INTERFACE)
add_library(toydep::toydep ALIAS toydep)
target_include_directories(toydep INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}/include")
