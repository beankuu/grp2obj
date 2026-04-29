@echo off
REM GRP Converter - Windows wrapper for main.py
REM Usage: grp2obj.bat <extracted_dir> [output_dir] [--verbose]

setlocal enabledelayedexpansion

if "%~1"=="" (
    echo Usage: grp2obj.bat ^<extracted_dir^> [output_dir] [--verbose]
    echo.
    echo Example:
    echo   grp2obj.bat ".\model_extracted"
    exit /b 1
)

set "SCRIPT_DIR=%~dp0"
set "EXTRACTED_DIR=%~1"
set "OUTPUT_DIR=%~2"
set "VERBOSE=%~3"

if "%OUTPUT_DIR%"=="" (
    set "OUTPUT_DIR=%EXTRACTED_DIR%\output"
)

REM Find Python
python --version >nul 2>&1
if errorlevel 1 (
    py --version >nul 2>&1
    if errorlevel 1 (
        echo Error: Python not found. Please install Python 3.x and ensure it's in PATH.
        exit /b 1
    )
    set "PYTHON_CMD=py"
) else (
    set "PYTHON_CMD=python"
)

REM Run converter
%PYTHON_CMD% "%SCRIPT_DIR%main.py" "%EXTRACTED_DIR%" "%OUTPUT_DIR%" %VERBOSE%

exit /b %errorlevel%
