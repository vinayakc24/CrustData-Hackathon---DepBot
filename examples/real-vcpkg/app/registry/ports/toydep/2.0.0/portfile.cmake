# Header-only demo port: installs the header and a CMake package exporting toydep::toydep.
set(VCPKG_POLICY_EMPTY_PACKAGE enabled)
file(INSTALL "${CMAKE_CURRENT_LIST_DIR}/toydep.hpp" DESTINATION "${CURRENT_PACKAGES_DIR}/include/toydep")
file(WRITE "${CURRENT_PACKAGES_DIR}/share/toydep/toydepConfig.cmake" [=[
if(NOT TARGET toydep::toydep)
  add_library(toydep::toydep INTERFACE IMPORTED)
  get_filename_component(_toydep_prefix "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
  set_target_properties(toydep::toydep PROPERTIES INTERFACE_INCLUDE_DIRECTORIES "${_toydep_prefix}/include")
endif()
]=])
file(WRITE "${CURRENT_PACKAGES_DIR}/share/toydep/copyright" "Demo package for DepBot. Public domain.\n")
