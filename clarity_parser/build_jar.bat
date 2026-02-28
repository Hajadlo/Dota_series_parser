@echo off
set JAVA_HOME=C:\Users\Hajad\.jdks\temurin-21.0.10
set PATH=%JAVA_HOME%\bin;%PATH%
call gradlew.bat jar --rerun-tasks
echo BUILD_EXIT_CODE=%ERRORLEVEL%
