@echo off
rem Builds the FlashRescue installer on this PC (the GitHub release workflow does the same).
rem Needs: Setup.bat run once (for the build environment), and Inno Setup 6 installed
rem (https://jrsoftware.org/isinfo.php).
setlocal
cd /d "%~dp0\.."
set "VPY=.build\venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Run Setup.bat first.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%v in (`"%VPY%" -c "import re;print(re.search(r\"VERSION = '([^']+)'\", open(r'src\FlashRescue.py', encoding='utf-8').read()).group(1))"`) do set "VER=%%v"
echo Building FlashRescue %VER% (folder version, for the installer)...
"%VPY%" -m PyInstaller --noconfirm --clean --log-level WARN --onedir --windowed --uac-admin ^
  --name FlashRescue --icon "%CD%\src\icon.ico" ^
  --add-data "%CD%\src\icon.ico;." --add-data "%CD%\src\icon.png;." ^
  --paths src --hidden-import linuxfs --hidden-import xfs --hidden-import f2fs --hidden-import lzo --hidden-import images --hidden-import encrypted --hidden-import fragments --hidden-import previews --hidden-import search --hidden-import recoverability --hidden-import zstandard --hidden-import argon2 --hidden-import _cffi_backend ^
  --distpath dist --workpath .build\work --specpath .build src\FlashRescue.py
if errorlevel 1 goto :fail
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 isn't installed - get it from https://jrsoftware.org/isinfo.php and run this again.
  pause
  exit /b 1
)
"%ISCC%" /Q /DAppVersion=%VER% /DSourceDir=..\dist\FlashRescue installer\FlashRescue.iss
if errorlevel 1 goto :fail
echo.
echo Done: dist\FlashRescue-Setup-%VER%.exe
pause
exit /b 0
:fail
echo.
echo Building the installer failed (see above).
pause
exit /b 1
