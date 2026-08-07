@echo off
setlocal EnableExtensions DisableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Could not open the project directory.
    exit /b 1
)

set "PROJECT_ROOT=%CD%"
set "VENV_DIR=%PROJECT_ROOT%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

echo =======================================
echo   Building video_agent executable
echo =======================================
echo Project: %PROJECT_ROOT%

if exist "%PYTHON_EXE%" goto :install_dependencies

echo [1/5] Creating .venv with Python 3.10 or newer...
py -3 -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv "%VENV_DIR%"
    goto :check_venv
)

python -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
    python -m venv "%VENV_DIR%"
    goto :check_venv
)

echo [ERROR] Python 3.10 or newer was not found.
echo Install Python, enable the Python launcher, then run this file again.
exit /b 1

:check_venv
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Failed to create .venv.
    exit /b 1
)

:install_dependencies
echo [2/5] Installing project dependencies...
"%PYTHON_EXE%" -m pip install -r "%PROJECT_ROOT%\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.txt.
    exit /b 1
)

echo [3/5] Checking PyInstaller...
"%PYTHON_EXE%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        exit /b 1
    )
)

for %%F in (
    "config.yaml"
    "config.example.yaml"
    "config\iectp_rsa_private.pem"
    "config\iectp_rsa_public.pem"
) do (
    if not exist "%PROJECT_ROOT%\%%~F" (
        echo [ERROR] Required runtime file is missing: %%~F
        exit /b 1
    )
)

set "DIST_DIR=%PROJECT_ROOT%\dist"
set "BUILD_DIR=%PROJECT_ROOT%\build\pyinstaller"
set "OUTPUT_EXE=%DIST_DIR%\video_agent.exe"

echo [4/5] Cleaning previous build artifacts...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%BUILD_DIR%" (
    echo [ERROR] Could not remove the previous build directory: %BUILD_DIR%
    exit /b 1
)
if exist "%OUTPUT_EXE%" del /f /q "%OUTPUT_EXE%"
if exist "%OUTPUT_EXE%" (
    echo [ERROR] Could not remove the previous EXE. Close video_agent.exe and try again.
    exit /b 1
)
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"
if errorlevel 1 (
    echo [ERROR] Could not create the dist directory.
    exit /b 1
)

echo [5/5] Building EXE...
"%PYTHON_EXE%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --console ^
  --name video_agent ^
  --distpath "%DIST_DIR%" ^
  --workpath "%BUILD_DIR%" ^
  --specpath "%BUILD_DIR%" ^
  --runtime-tmpdir "%TEMP%\VideoAgentRuntime" ^
  --add-data "%PROJECT_ROOT%\config.yaml;." ^
  --add-data "%PROJECT_ROOT%\config.example.yaml;." ^
  --add-data "%PROJECT_ROOT%\config\iectp_rsa_private.pem;config" ^
  --add-data "%PROJECT_ROOT%\config\iectp_rsa_public.pem;config" ^
  --collect-submodules pywinauto ^
  --collect-submodules obsws_python ^
  --collect-submodules minio ^
  --collect-submodules cryptography ^
  --hidden-import video_agent.agent ^
  --hidden-import video_agent.providers.tencent_meeting ^
  --hidden-import video_agent.providers.dingtalk ^
  --hidden-import video_agent.providers.mixlink ^
  --hidden-import video_agent.providers.douyin_live ^
  --hidden-import video_agent.providers.wechat_live ^
  "%PROJECT_ROOT%\video_agent\agent.py"
if errorlevel 1 (
    echo [ERROR] PyInstaller failed.
    exit /b 1
)

if not exist "%OUTPUT_EXE%" (
    echo [ERROR] Build finished without producing video_agent.exe.
    exit /b 1
)

echo.
echo Build completed successfully.
echo EXE: "%OUTPUT_EXE%"
echo Runtime folder on first launch: "%USERPROFILE%\Desktop\VideoAgent"
exit /b 0
