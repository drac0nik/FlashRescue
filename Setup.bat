@echo off
setlocal
title FlashRescue setup
cd /d "%~dp0"
echo.
echo  ===== FlashRescue setup =====
echo  Builds FlashRescue.exe in this folder. Takes a minute or two the first time.
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul || goto nopython
%PY% -c "import tkinter" >nul 2>nul || (
    echo  Python is installed without tkinter. Re-run the Python installer, choose Modify,
    echo  and tick "tcl/tk and IDLE".
    goto fail
)

echo  [1/4] Creating build environment...
if not exist ".build\venv\Scripts\python.exe" (
    %PY% -m venv ".build\venv" || goto fail
)
set "VPY=.build\venv\Scripts\python.exe"

echo  [2/4] Installing Pillow and PyInstaller...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install --quiet --disable-pip-version-check pillow pyinstaller cryptography zstandard argon2-cffi || goto fail

echo  [3/4] Building FlashRescue.exe...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
    --name FlashRescue --icon "%CD%\src\icon.ico" ^
    --add-data "%CD%\src\icon.ico;." --add-data "%CD%\src\icon.png;." ^
    --paths src --hidden-import linuxfs --hidden-import xfs --hidden-import f2fs --hidden-import lzo --hidden-import images --hidden-import encrypted --hidden-import fragments --hidden-import previews --hidden-import search --hidden-import recoverability --hidden-import zstandard --hidden-import argon2 --hidden-import _cffi_backend ^
    --distpath "." --workpath ".build\work" --specpath ".build" ^
    "src\FlashRescue.py" || goto fail

echo  [4/4] Getting ffmpeg (used to check and repair videos)...
if exist "tools\ffmpeg.exe" if exist "tools\ffprobe.exe" (
    echo        already there.
    goto ffmpeg_done
)
if not exist "tools" mkdir "tools"
curl.exe -L --fail --silent --show-error -o ".build\ffmpeg.zip" "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
if errorlevel 1 goto ffmpeg_failed
if exist ".build\ffmpeg" rmdir /s /q ".build\ffmpeg"
powershell -NoProfile -Command "Expand-Archive -Force '.build\ffmpeg.zip' '.build\ffmpeg'"
if errorlevel 1 goto ffmpeg_failed
for /r ".build\ffmpeg" %%F in (ffmpeg*.exe ffprobe*.exe) do copy /y "%%F" "tools\" >nul
if not exist "tools\ffmpeg.exe" goto ffmpeg_failed
echo        done.
goto ffmpeg_done
:ffmpeg_failed
echo        Could not download ffmpeg. Everything else works; for video checking and repair,
echo        put ffmpeg.exe and ffprobe.exe in the "tools" folder next to FlashRescue.exe.
:ffmpeg_done

echo.
echo  Done! Double-click FlashRescue.exe to start it (Windows will ask for admin rights,
echo  which it needs to read the drive directly).
echo.
pause
exit /b 0

:nopython
echo  Python 3.9 or newer was not found.
echo  Install it from https://www.python.org/downloads/  (tick "Add python.exe to PATH"),
echo  then run Setup.bat again.
:fail
echo.
echo  Setup did not finish - see the message above.
pause
exit /b 1
