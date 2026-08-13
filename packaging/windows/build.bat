@echo off
rem ===========================================================================
rem  Internet Xtreme Downloader — build for Windows
rem
rem  Double-click it, or run it from a Command Prompt. Either works: the first
rem  thing it does is move to the repository root, because double-clicking a
rem  script from Explorer starts it wherever Explorer happened to be and every
rem  relative path after that points at nothing.
rem
rem  If Python is missing it is installed — through winget, or Chocolatey when
rem  winget is not available. Nothing else is touched.
rem
rem  Everything it prints is also written to build-windows.log. If the build
rem  fails, that file is the thing to send back.
rem ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

set "LOG=%CD%\build-windows.log"
echo Internet Xtreme Downloader - Windows build > "%LOG%"
echo Started %DATE% %TIME% >> "%LOG%"
echo. >> "%LOG%"

call :say "Working in %CD%"

rem --- find a Python ---------------------------------------------------------
call :findpython
if not defined PY (
  call :say "Python was not found. Installing it..."
  where winget >nul 2>&1
  if not errorlevel 1 (
    call :say "Using winget. Accept the prompt if one appears."
    winget install -e --id Python.Python.3.13 --scope user ^
      --accept-source-agreements --accept-package-agreements >> "%LOG%" 2>&1
  ) else (
    where choco >nul 2>&1
    if not errorlevel 1 (
      call :say "Using Chocolatey."
      choco install -y python313 >> "%LOG%" 2>&1
    ) else (
      call :say "ERROR: neither winget nor Chocolatey is available, so Python"
      call :say "cannot be installed automatically."
      call :say ""
      call :say "Install it from https://www.python.org/downloads/windows/"
      call :say "and tick 'Add python.exe to PATH', then run this again."
      goto :failed
    )
  )
  rem A fresh install is on the PATH of *new* processes, not this one. The
  rem per-user install location is predictable, so it is looked for directly
  rem before giving up and asking for a new Command Prompt.
  call :findpython
  if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
      if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
  )
  if not defined PY (
    call :say "Python was installed but is not visible to this window yet."
    call :say "Close this window, open a new one, and run the script again."
    goto :failed
  )
)

call :say "Using: %PY%"
%PY% -c "import sys,platform;print('Python',sys.version.split()[0],platform.architecture()[0])" >> "%LOG%" 2>&1
%PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,11) and sys.maxsize>2**32 else 1)"
if errorlevel 1 (
  call :say "ERROR: a 64-bit Python 3.11 or newer is required."
  call :say "Check the line above in build-windows.log for what was found."
  goto :failed
)

rem --- environment -----------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  call :say "Creating the environment (this happens once)..."
  %PY% -m venv .venv >> "%LOG%" 2>&1
  if errorlevel 1 ( call :say "ERROR: could not create .venv" & goto :failed )
)

set "VPY=%CD%\.venv\Scripts\python.exe"
if not exist "%VPY%" ( call :say "ERROR: %VPY% is missing" & goto :failed )

call :say "Installing dependencies..."
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  call :say "That exact PySide6 has no wheel for this Python; trying the latest."
  "%VPY%" -m pip install PySide6 pyinstaller >> "%LOG%" 2>&1
  if errorlevel 1 ( call :say "ERROR: dependencies would not install" & goto :failed )
)
"%VPY%" -c "import PySide6,PyInstaller;print('PySide6',PySide6.__version__,'PyInstaller',PyInstaller.__version__)" >> "%LOG%" 2>&1

rem --- build -----------------------------------------------------------------
call :say "Building (a few minutes)..."
"%VPY%" "%CD%\packaging\build.py" --package >> "%LOG%" 2>&1
if errorlevel 1 (
  call :say "ERROR: the build failed. build-windows.log has the reason."
  goto :failed
)

rem --- did it actually produce anything? -------------------------------------
set "OK=1"
if not exist "dist\ixd\ixd.exe" (
  call :say "MISSING: dist\ixd\ixd.exe"
  set "OK=0"
)
if not exist "dist\ixd-extension-chrome-1.0.0.zip" (
  call :say "MISSING: dist\ixd-extension-chrome-1.0.0.zip"
  set "OK=0"
)
if "!OK!"=="0" (
  call :say "The build reported success but the files are not there."
  goto :failed
)

rem --- browser bridge --------------------------------------------------------
call :say "Registering the browser bridge..."
"%VPY%" "%CD%\native-host\install_host.py" >> "%LOG%" 2>&1
"%VPY%" "%CD%\native-host\install_host.py" --verify >> "%LOG%" 2>&1

call :say ""
call :say "Done."
call :say "  Application : dist\ixd\ixd.exe"
call :say "  To send on  : dist\ixd-1.0.0-windows-x64.zip"
call :say "  Extension   : dist\ixd-extension-chrome-1.0.0.zip"
call :say ""
call :say "Load the extension: chrome://extensions - Developer mode -"
call :say "Load unpacked - pick the 'extension' folder here."
echo.
if not defined CI pause
endlocal
exit /b 0

:findpython
set "PY="
rem An explicit choice wins over discovery. Continuous integration pins the
rem interpreter it has just installed, and `py -3` on a machine with several
rem Pythons registered answers with whichever the registry ranks highest —
rem which is not necessarily the one that was meant.
if defined IXD_PYTHON (
  if exist "%IXD_PYTHON%" set "PY=%IXD_PYTHON%"
  if defined PY exit /b 0
)
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
rem Windows ships an "app execution alias" called python.exe that does nothing
rem but open the Microsoft Store. It answers `where`, so it has to be run to be
rem told apart from a real interpreter.
if defined PY (
  %PY% -c "import sys" >nul 2>&1 || set "PY="
)
exit /b 0

:failed
call :say ""
rem The tail of the log, because "the reason is in a file" is no use to anyone
rem reading this over a CI runner's shoulder — and not much use to a person.
if exist "%LOG%" (
  call :say "--- end of build-windows.log ---"
  powershell -NoProfile -Command "Get-Content -Tail 40 '%LOG%'" 2>nul
)
call :say ""
call :say "Nothing was built. Send build-windows.log back."
echo.
if not defined CI pause
endlocal
exit /b 1

:say
echo %~1
echo %~1 >> "%LOG%"
exit /b 0
