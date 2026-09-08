@echo off
setlocal

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "VENV_DIR=%ROOT_DIR%\.venv"
set "REQ_FILE=%ROOT_DIR%\requirements.txt"
set "STAMP_FILE=%VENV_DIR%\.requirements.sha256"

if exist "%VENV_DIR%\Scripts\python.exe" (
  set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
) else (
  where py >NUL 2>NUL
  if %ERRORLEVEL% EQU 0 (
    py -3 -m venv "%VENV_DIR%"
  ) else (
    python -m venv "%VENV_DIR%"
  )
  if errorlevel 1 exit /b 1
  set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
)

for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash '%REQ_FILE%' -Algorithm SHA256).Hash"`) do set "REQ_HASH=%%H"
if exist "%STAMP_FILE%" (
  set /p INSTALLED_HASH=<"%STAMP_FILE%"
)

if not "%REQ_HASH%"=="%INSTALLED_HASH%" (
  "%PYTHON_EXE%" -m pip install --upgrade pip
  if errorlevel 1 exit /b 1
  "%PYTHON_EXE%" -m pip install -r "%REQ_FILE%"
  if errorlevel 1 exit /b 1
  >"%STAMP_FILE%" echo %REQ_HASH%
)

pushd "%ROOT_DIR%"
"%PYTHON_EXE%" -m blink_light %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
