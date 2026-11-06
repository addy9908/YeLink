@echo off
REM ==============================================================
REM  YeLink build script - builds both YeLink programs
REM    1. YeLink_AdapterUI.exe  (C#, GUI, uses csc from .NET Framework)
REM    2. YeLink_Controller.exe (native C, uses gcc from w64devkit)
REM  Place this file in the same folder as the source files.
REM ==============================================================
setlocal
cd /d "%~dp0"

set CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe
set W64=C:\w64devkit\bin
set ICON=YeLink.ico

REM ---------- 1. Adapter GUI (C#) ----------
echo ==== [1/2] Building YeLink_AdapterUI.exe ====
if not exist "%CSC%" (
    echo ERROR: csc.exe not found at %CSC%
    goto :fail
)
if exist "%ICON%" (
    "%CSC%" /nologo /target:winexe /out:YeLink_AdapterUI.exe /win32icon:%ICON% /reference:System.Windows.Forms.dll /reference:System.Drawing.dll YeLink_AdapterUI.cs
) else (
    echo [warn] %ICON% not found - building adapter without icon
    "%CSC%" /nologo /target:winexe /out:YeLink_AdapterUI.exe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll YeLink_AdapterUI.cs
)
if errorlevel 1 goto :fail

REM ---------- 2. Controller (native C) ----------
echo.
echo ==== [2/2] Building YeLink_Controller.exe ====
set PATH=%W64%;%PATH%
where gcc >nul 2>nul
if errorlevel 1 (
    echo ERROR: gcc not found. Install w64devkit to C:\w64devkit - see the instruction file.
    goto :fail
)
if exist "%ICON%" (
    echo 1 ICON "%ICON%" > icon.rc
    windres icon.rc -o icon.o
    gcc YeLink_Controller.c icon.o -o YeLink_Controller.exe -lws2_32 -mwindows -O2 -s
) else (
    echo [warn] %ICON% not found - building controller without icon
    gcc YeLink_Controller.c -o YeLink_Controller.exe -lws2_32 -mwindows -O2 -s
)
if errorlevel 1 goto :fail
del icon.rc icon.o 2>nul

echo.
echo ==== BUILD OK ====
echo   YeLink_AdapterUI.exe
echo   YeLink_Controller.exe
pause
exit /b 0

:fail
echo.
echo ==== BUILD FAILED - see messages above ====
pause
exit /b 1
