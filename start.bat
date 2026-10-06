@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
  echo [Karya] First run: creating Python environment...
  py -3.11 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
if not exist ".env" copy ".env.example" ".env" >nul

rem Make sure the offline model server is up (ignored if Ollama is not installed)
where ollama >nul 2>nul && (
  tasklist /FI "IMAGENAME eq ollama.exe" | find /I "ollama.exe" >nul || start "" /B ollama serve >nul 2>nul
)

if /I "%1"=="cli" (
  ".venv\Scripts\python.exe" -m karya.cli
) else (
  ".venv\Scripts\python.exe" -m karya.server %*
)
if errorlevel 1 pause
goto :eof

:fail
echo [Karya] Setup failed. Check your internet connection and try again.
pause
