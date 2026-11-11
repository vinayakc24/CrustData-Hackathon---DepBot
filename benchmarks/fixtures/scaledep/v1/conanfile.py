import os

from conan import ConanFile
from conan.tools.files import copy


class Recipe(ConanFile):
    name = "scaledep"
    version = "1.0.0"
    package_type = "header-library"
    exports_sources = "include/*"

    def package(self):
        copy(self, "*.hpp", src=os.path.join(self.source_folder, "include"),
             dst=os.path.join(self.package_folder, "include"))

    def package_id(self):
        self.info.clear()

    def package_info(self):
        self.cpp_info.bindirs = []
        self.cpp_info.libdirs = []
