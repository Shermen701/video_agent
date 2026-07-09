@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    set "PYTHON_EXE=%VENV_PYTHON%"
) else (
    set "PYTHON_EXE=python"
)

echo =======================================
echo   Building video_agent executable
echo =======================================
echo Using Python: %PYTHON_EXE%

"%PYTHON_EXE%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found, installing...
    "%PYTHON_EXE%" -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

if not exist "%CD%\dist" mkdir "%CD%\dist"
if not exist "%CD%\build" mkdir "%CD%\build"

echo Cleaning previous build artifacts...
if exist "%CD%\build\pyinstaller" rmdir /s /q "%CD%\build\pyinstaller"
if exist "%CD%\dist\video_agent.exe" del /f /q "%CD%\dist\video_agent.exe"

echo Running PyInstaller...
"%PYTHON_EXE%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --console ^
  --name video_agent ^
  --distpath "%CD%\dist" ^
  --workpath "%CD%\build\pyinstaller" ^
  --specpath "%CD%\build\pyinstaller" ^
  --runtime-tmpdir "%TEMP%\VideoAgentRuntime" ^
  --add-data "%CD%\config.yaml;." ^
  --add-data "%CD%\config.example.yaml;." ^
  --add-data "%CD%\config\iectp_rsa_private.pem;config" ^
  --add-data "%CD%\config\iectp_rsa_public.pem;config" ^
  --collect-submodules pywinauto ^
  --collect-submodules obsws_python ^
  --collect-submodules minio ^
  --collect-submodules cryptography ^
  --hidden-import video_agent.providers.tencent_meeting ^
  --hidden-import video_agent.agent ^
  "%CD%\video_agent\agent.py"

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build completed successfully.
echo EXE: "%CD%\dist\video_agent.exe"
echo Runtime folder: "%USERPROFILE%\Desktop\VideoAgent"
exit /b 0
