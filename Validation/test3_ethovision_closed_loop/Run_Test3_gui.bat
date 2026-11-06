@echo off
rem Run_Test3_gui.bat -- launch test3_closedloop_gui.py with the Anaconda Python, from this folder.
rem Put this file next to test3_closedloop_gui.py. Results go to results\ in the same folder.
rem Edit ANACONDA below if your Anaconda/Miniconda is installed somewhere else.

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
    python "%~dp0test3_closedloop_gui.py"
) else (
    echo Anaconda not found in the usual places; trying python on PATH.
    python "%~dp0test3_closedloop_gui.py"
)

if errorlevel 1 (
    echo.
    echo The GUI exited with an error. Check the messages above.
    pause
)
endlocal
