@echo off
set CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe

echo Building ZY_PinBridge_UI.exe ...
"%CSC%" /target:winexe /out:ZY_PinBridge_UI.exe /win32icon:ZY_PinBridge.ico /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ZY_PinBridge_UI.cs

echo Building ZY_PinBridge_Controller.exe ...
"%CSC%" /target:exe /out:ZY_PinBridge_Controller.exe /win32icon:ZY_PinBridge.ico ZY_PinBridge_Controller.cs

echo.
echo Done.
pause