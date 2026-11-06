# YeLink - Build and Setup Instructions

**YeLink** - Your Experiment Link. An interface that turns software commands into hardware actions, connecting EthoVision and other experiment software to external devices.

## Files in the project folder

| File | Purpose |
|---|---|
| `YeLink_AdapterUI.cs` | Source for the adapter GUI (C#) |
| `YeLink_Controller.c` | Source for the controller (native C) |
| `YeLink.ico` | Icon embedded into both EXEs (optional) |
| `YeLink_build_ui.bat` | Builds both EXEs in one step (rename to `build_ui.bat` if you prefer) |
| Arduino firmware (`.ino`) | Uploaded to the Uno with the Arduino IDE |

Only `YeLink_AdapterUI.exe` and `YeLink_Controller.exe` are needed on the lab PC. Keep the source files and build script for future changes.

## One-time: install the C compiler (w64devkit)

w64devkit is a free, portable C compiler for Windows. It is a folder, not an installer, so no admin rights are needed.

1. Open the releases page: **https://github.com/skeeto/w64devkit/releases**
2. Download the latest file named `w64devkit-x64-<version>.7z.exe` (for example `w64devkit-x64-2.10.1.7z.exe`).
3. Double-click it. When asked where to extract, enter `C:\w64devkit`.
4. If Windows SmartScreen warns, click **More info**, then **Run anyway**.

After extraction, `C:\w64devkit\bin\gcc.exe` should exist. The build script expects it there.

## One-time: install the firmware

1. Open the Arduino IDE, select **Arduino Uno** and the correct COM port.
2. Upload the firmware. Make sure the baud rate in the firmware is **115200**.
3. Close the Arduino IDE Serial Monitor before running YeLink, since only one program can hold the COM port.

## Every time you change the source: build

1. Put the build script in the same folder as `YeLink_AdapterUI.cs` and `YeLink_Controller.c`.
2. Double-click the build script.
3. Look for `==== BUILD OK ====`. The two EXEs appear in the same folder.

If you see `BUILD FAILED`, read the message above it. Common causes:

- `csc.exe not found`: the .NET Framework path differs on this PC. Find the `v4.x` folder under `C:\Windows\Microsoft.NET\Framework64\` and update the `CSC=` line.
- `gcc not found`: w64devkit is not at `C:\w64devkit`. Re-extract it there or edit the `W64=` line.
- Icon messages: a `[warn]` line only means the icon was skipped. Put `YeLink.ico` next to the script to include it.

## Daily use

1. Plug in the Arduino and note its COM port in Device Manager.
2. Double-click `YeLink_AdapterUI.exe`. Select the COM port and baud **115200**, then click **Connect**. Wait for the green **Connected** status.
3. Leave the YeLink window open for the whole session.
4. In EthoVision, set the External Command action:
   - **Select program to run:** full path to `YeLink_Controller.exe`
   - **Command line options:** for example `p=8,v=1,d=5` or `p=8,v=1,d=5;p=7,v=1,d=10`
5. Each command should appear in the log within a few tens of milliseconds. Export the CSV from YeLink when the session ends.
6. Close YeLink last. It turns all pins off automatically on close.

## Command reference

- `p` = digital pin (2 to 13)
- `v` = 1 for HIGH, 0 for LOW
- `d` = duration in seconds, applies only when `v=1`. `d=0` latches the pin on.
- `;` separates multiple commands in one line.

## Keeping the setup portable

Only the two EXEs and `YeLink.ico` need to be copied to another PC. The source files are needed only to rebuild. If antivirus blocks the EXEs, add the folder to the exclusions list or ask IT to whitelist it.
