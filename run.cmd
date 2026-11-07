@echo off
rem Entry point. Usage: run.cmd doctor ^| demo ^| tools ^| challenges ^| state ^| solve ^<id^>
rem Keep this file pure ASCII: cmd.exe mis-parses UTF-8 comments under legacy code pages.
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

set "PY=%DSH_PYTHON%"
if not defined PY set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m ctfy_agent %*
exit /b %ERRORLEVEL%
