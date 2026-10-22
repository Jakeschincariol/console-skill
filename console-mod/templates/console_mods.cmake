# Console skill (console-mod): compiles every override .cpp in this folder
# into ps2EntryRunner, the PS2Recomp port executable.
#
# Add ONE line at the END of ps2xRuntime/CMakeLists.txt in your PS2Recomp
# checkout (after the ps2EntryRunner target exists):
#   include("/full/path/to/console/patches/console_mods.cmake" OPTIONAL)
# Then rebuild incrementally: cmake --build out/build --config Debug
#
# Each file is kept out of the runner's unity batches, so adding a mod does
# not rebuild the recompiled game code, and it is linked straight into the
# executable, so its PS2_REGISTER_GAME_OVERRIDE registration always runs.
# Remove the include line (or the .cpp) to turn the mods off.
file(GLOB CONSOLE_MOD_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_LIST_DIR}/*.cpp")
if(TARGET ps2EntryRunner AND CONSOLE_MOD_SOURCES)
    target_sources(ps2EntryRunner PRIVATE ${CONSOLE_MOD_SOURCES})
    set_source_files_properties(${CONSOLE_MOD_SOURCES} PROPERTIES SKIP_UNITY_BUILD_INCLUSION TRUE)
    message(STATUS "Console skill mods: ${CONSOLE_MOD_SOURCES}")
endif()
