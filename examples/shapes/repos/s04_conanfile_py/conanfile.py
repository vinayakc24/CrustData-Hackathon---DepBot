from conan import ConanFile

class App(ConanFile):
    settings = 'os', 'arch', 'compiler', 'build_type'
    requires = 'toydep/1.0.0'
    generators = 'CMakeDeps', 'CMakeToolchain'
