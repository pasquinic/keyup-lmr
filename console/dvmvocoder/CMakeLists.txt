# SPDX-License-Identifier: GPL-2.0-only
#/*
# * Digital Voice Modem Shared Vocoder - CMake Build System
# * GPLv2 Open Source. Use is subject to license terms.
# * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
# *
# *  Copyright (C) 2024 Bryan Biedenkapp, N2PLL
# *  Copyright (C) 2025 Patrick McDonnell, W3AXL
# *
# */

cmake_minimum_required(VERSION 3.16.0)

project(dvmvocoder)

set(CMAKE_STATIC_LIBRARY_PREFIX "")
set(CMAKE_SHARED_LIBRARY_PREFIX "")

set(WINDOWS_EXPORT_ALL_SYMBOLS true)

#
# Build Vocoder as a Shared Library (dll or so)
#
include(vocoder/CMakeLists.txt)
add_library(libvocoder SHARED ${vocoder_SRC} ${vocoder_INCLUDE})
target_include_directories(libvocoder PRIVATE vocoder)