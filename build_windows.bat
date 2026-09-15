@echo off
REM === NevisarMic — build Windows .exe (run on Windows) ===
REM Needs: Python 3.10+ (with tkinter) + ffmpeg installed separately
REM Steps: double-click this file

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install from https://www.python.org/downloads/ ^(tick "Add python to PATH"^)
  pause
  exit /b 1
)

echo [*] Installing PyInstaller...
python -m pip install --upgrade pip
python -m pip install pyinstaller

echo [*] Building NevisarMic.exe ...
python -m PyInstaller --noconfirm NevisarMic.spec

echo.
if exist "dist\NevisarMic.exe" (
  echo [OK] Built: dist\NevisarMic.exe
  echo Copy ffmpeg.exe next to NevisarMic.exe if target PCs have no ffmpeg,
  echo or run: winget install Gyan.FFmpeg
) else (
  echo [ERROR] Build failed. See output above.
)
pause
