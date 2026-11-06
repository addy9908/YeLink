@echo off

setlocal
cd /d "%~dp0"

set "ANACONDA="
if exist "%USERPROFILE%\anaconda3\python.exe"      set "ANACONDA=%USERPROFILE%\anaconda3"
if exist "%USERPROFILE%\miniconda3\python.exe"     set "ANACONDA=%USERPROFILE%\miniconda3"
if exist "%LOCALAPPDATA%\anaconda3\python.exe"     set "ANACONDA=%LOCALAPPDATA%\anaconda3"
if exist "%LOCALAPPDATA%\miniconda3\python.exe"    set "ANACONDA=%LOCALAPPDATA%\miniconda3"
if exist "C:\ProgramData\anaconda3\python.exe"     set "ANACONDA=C:\ProgramData\anaconda3"
if exist "C:\ProgramData\miniconda3\python.exe"    set "ANACONDA=C:\ProgramData\miniconda3"

if defined ANACONDA (
    echo Using Anaconda at %ANACONDA%
    call "%ANACONDA%\Scripts\activate.bat" "%ANACONDA%"
    python "%~dp0Validation4_multiarena_gui.py"
) else (
    echo Anaconda not found in the usual places; trying python on PATH.
    python "%~dp0Validation4_multiarena_gui.py"
)

if errorlevel 1 (
    echo.
    echo The GUI exited with an error. Check the messages above.
    pause
)
endlocal
