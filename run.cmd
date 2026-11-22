@echo off
setlocal
chcp 65001 >nul
title DSH Chat Manager
cd /d "%~dp0"

set "PYTHON_EXE="
set "PYTHONUTF8=1"
where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_EXE=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
  echo Python was not found.
  echo Install Python 3.9+ from https://www.python.org/downloads/
  echo and enable "Add python.exe to PATH".
  pause
  exit /b 1
)

%PYTHON_EXE% dsh_chat_manager.py %*
if errorlevel 1 (
  echo.
  echo The program exited with an error.
  pause
)

endlocal
