cmake_minimum_required(VERSION 3.15)
project(example_soc)

# Find Python and pybind11
find_package(Python COMPONENTS Interpreter Development REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

# Create the Python extension module
pybind11_add_module(_example_soc_native example_soc_bindings.cpp)

# Set C++ standard
target_compile_features(_example_soc_native PRIVATE cxx_std_11)

# Include directories
target_include_directories(_example_soc_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# Install the module
install(TARGETS _example_soc_native LIBRARY DESTINATION .)