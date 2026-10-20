@echo off
setlocal EnableExtensions
title Sentrion - One Click Setup and Run
cd /d "%~dp0"

echo ============================================================
echo  SENTRION - ONE CLICK SETUP AND RUN
echo ============================================================
echo.

if not exist "app\main.py" (
  echo [ERROR] File app\main.py was not found.
  echo Please extract this ZIP and run the BAT from the Sentrion_MVP folder.
  pause
  exit /b 1
)
if not exist "requirements.txt" (
  echo [ERROR] requirements.txt was not found.
  pause
  exit /b 1
)

if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "SENTRION_CACHE=%LOCALAPPDATA%\Sentrion"
if not exist "%SENTRION_CACHE%" mkdir "%SENTRION_CACHE%" >nul 2>&1

 echo [1/6] Checking Python 3.11 or newer...
set "BASE_PY="
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%P"
)
if defined BASE_PY (
  "%BASE_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
  if errorlevel 1 set "BASE_PY="
)
if not defined BASE_PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%P"
  )
)
if not defined BASE_PY (
  echo [ERROR] Python 3.11 or newer was not found.
  echo Install Python from https://www.python.org/downloads/windows/ and enable the Python PATH option.
  pause
  exit /b 1
)
"%BASE_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.11 or newer is required. Found an older Python version.
  pause
  exit /b 1
)
REM Avoid nested quotes inside FOR /F; this breaks when Python is installed under a quoted path.
set "PY_MINOR="
set "PYVER_FILE=%TEMP%\sentrion_python_version_%RANDOM%_%RANDOM%.tmp"
"%BASE_PY%" -c "import sys; print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))" > "%PYVER_FILE%" 2>nul
if errorlevel 1 (
  if exist "%PYVER_FILE%" del "%PYVER_FILE%" >nul 2>&1
  echo [ERROR] Could not determine Python version using the selected interpreter.
  pause
  exit /b 1
)
if exist "%PYVER_FILE%" (
  set /p PY_MINOR=<"%PYVER_FILE%"
  del "%PYVER_FILE%" >nul 2>&1
)
if not defined PY_MINOR (
  echo [ERROR] Could not determine Python version.
  pause
  exit /b 1
)
echo [OK] Python %PY_MINOR% found.

set "RUNTIME_DIR=%SENTRION_CACHE%\python-%PY_MINOR%"
set "VENV_DIR=%RUNTIME_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "STAMP_FILE=%RUNTIME_DIR%\requirements.sha256"
set "REQ_PATH=%CD%\requirements.txt"
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%" >nul 2>&1
if not exist "%SENTRION_CACHE%\pip-cache" mkdir "%SENTRION_CACHE%\pip-cache" >nul 2>&1

 echo [2/6] Checking the reusable Sentrion Python environment...
if not exist "%VENV_PY%" (
  echo Creating a reusable virtual environment. This step runs only once per Python version.
  "%BASE_PY%" -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Could not create the virtual environment.
    pause
    exit /b 1
  )
) else (
  echo [OK] Reusing the existing virtual environment.
)

 echo [3/6] Checking dependency fingerprint...
set "REQ_HASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath $env:REQ_PATH).Hash" 2^>nul`) do set "REQ_HASH=%%H"
if not defined REQ_HASH (
  echo [ERROR] Could not calculate the requirements fingerprint. PowerShell is required on Windows.
  pause
  exit /b 1
)
set "OLD_HASH="
if exist "%STAMP_FILE%" set /p OLD_HASH=<"%STAMP_FILE%"
if /i "%REQ_HASH%"=="%OLD_HASH%" (
  "%VENV_PY%" -c "import fastapi, uvicorn, jinja2, multipart, itsdangerous, fitz, pytesseract, PIL, openpyxl" >nul 2>&1
  if not errorlevel 1 (
    echo [OK] Dependencies are already installed and unchanged. Skipping pip install.
    goto DEPENDENCIES_READY
  )
  echo [INFO] A required package is missing; repairing the environment.
)

echo Installing or updating runtime dependencies. Pip's download cache will be reused.
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
"%VENV_PY%" -m pip install --cache-dir "%SENTRION_CACHE%\pip-cache" -r "%CD%\requirements.txt"
if errorlevel 1 (
  echo [ERROR] Dependency installation failed. Check your internet connection and requirements.txt.
  pause
  exit /b 1
)
"%VENV_PY%" -m pip check
if errorlevel 1 (
  echo [ERROR] pip detected an incompatible dependency set. Review the output above.
  pause
  exit /b 1
)
"%VENV_PY%" -c "import fastapi, uvicorn, jinja2, multipart, itsdangerous, fitz, pytesseract, PIL, openpyxl" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] One or more required packages failed the import check.
  pause
  exit /b 1
)
>"%STAMP_FILE%" echo %REQ_HASH%
echo [OK] Runtime dependencies are ready.

:DEPENDENCIES_READY

 echo [4/6] Preparing local configuration...
if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
    "%VENV_PY%" -c "from pathlib import Path; import secrets; p=Path('.env'); s=p.read_text(encoding='utf-8'); s=s.replace('replace-with-a-long-random-secret', secrets.token_urlsafe(48)); p.write_text(s, encoding='utf-8')"
    echo [OK] Created .env with a randomly generated local session secret.
  ) else (
    echo [WARN] .env.example was not found; application defaults will be used.
  )
) else (
  echo [OK] Existing .env preserved; it was not overwritten.
)

 echo [5/6] Checking Tesseract OCR...
where tesseract >nul 2>&1
if errorlevel 1 (
  if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "PATH=%PATH%;%ProgramFiles%\Tesseract-OCR"
)
where tesseract >nul 2>&1
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Tesseract-OCR"
)
where tesseract >nul 2>&1
if errorlevel 1 (
  where winget >nul 2>&1
  if not errorlevel 1 (
    echo Tesseract is missing. Attempting installation with Windows Package Manager...
    winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements --silent
    if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "PATH=%PATH%;%ProgramFiles%\Tesseract-OCR"
    if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Tesseract-OCR"
  )
)
where tesseract >nul 2>&1
if errorlevel 1 (
  echo [WARN] Tesseract was not found. The app can still run, but scanned PDFs/images may fail OCR.
  echo        Install Tesseract OCR and ensure tesseract.exe is in PATH for full document support.
) else (
  echo [OK] Tesseract OCR is available.
)

 echo [6/6] Starting Sentrion...
REM Reuse an already-running Sentrion service rather than launching a duplicate server.
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2; if ($r.app -eq 'Sentrion' -and $r.status -eq 'ok') { exit 0 } } catch { }; exit 1" >nul 2>&1
if not errorlevel 1 (
  echo [OK] Sentrion is already running at http://127.0.0.1:8000
  start "" "http://127.0.0.1:8000"
  exit /b 0
)
start "Sentrion Server" /D "%CD%" "%VENV_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo Waiting for the local health endpoint...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$limit=(Get-Date).AddSeconds(60); while ((Get-Date) -lt $limit) { try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2; if ($r.app -eq 'Sentrion' -and $r.status -eq 'ok') { exit 0 } } catch { }; Start-Sleep -Seconds 1 }; exit 1" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Sentrion did not respond within 60 seconds.
  echo Check the separate "Sentrion Server" window for the error message.
  echo If port 8000 is occupied by another application, close that application and try again.
  pause
  exit /b 1
)
echo [OK] Sentrion is ready. Opening the browser...
start "" "http://127.0.0.1:8000"
echo.
echo Keep the "Sentrion Server" window open while using Sentrion.
echo To stop the server, focus that window and press Ctrl+C.
echo Dependencies are cached under %%LOCALAPPDATA%%\Sentrion and will not be reinstalled on every build.
timeout /t 5 >nul
exit /b 0
