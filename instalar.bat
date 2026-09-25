@echo off
setlocal
cd /d "%~dp0"
title Instalando Transcriptor Local

echo ============================================================
echo   Instalacion de Transcriptor Local
echo   Tarda unos minutos y necesita internet (~1.5 GB en total).
echo   Todo se instala dentro de esta carpeta (.venv, tools, models);
echo   para desinstalar basta con borrar la carpeta.
echo ============================================================
echo.

rem --- 1. Python (3.12 recomendado); solo se usa para crear el entorno virtual ---
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [1/4] Instalando Python 3.12...
    winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
    set "PY=py -3.12"
) else (
    echo [1/4] Python encontrado: %PY%
)

rem --- 2. Entorno virtual y librerias (en .venv, nunca en el Python global) ---
echo [2/4] Instalando librerias en el entorno virtual .venv...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 goto :error
rem Sin cache global de pip: nada se escribe fuera de esta carpeta.
set "PIP_NO_CACHE_DIR=1"
set "PIP_REQUIRE_VIRTUALENV=1"
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 goto :error

rem --- 3. ffmpeg portable en tools\ffmpeg (no modifica el PATH del sistema) ---
if exist "tools\ffmpeg\bin\ffmpeg.exe" (
    echo [3/4] ffmpeg ya esta instalado en tools\ffmpeg.
) else (
    echo [3/4] Descargando ffmpeg portable a tools\ffmpeg...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; New-Item -ItemType Directory -Force tools | Out-Null; Remove-Item tools\_ffmpeg, tools\ffmpeg.zip, tools\ffmpeg -Recurse -Force -ErrorAction SilentlyContinue; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile tools\ffmpeg.zip; Expand-Archive tools\ffmpeg.zip tools\_ffmpeg; Move-Item (Get-ChildItem tools\_ffmpeg -Directory)[0].FullName tools\ffmpeg; Remove-Item tools\_ffmpeg, tools\ffmpeg.zip -Recurse -Force"
    if not exist "tools\ffmpeg\bin\ffmpeg.exe" goto :error
)

rem --- 4. Modelo de voz en models\ (una sola vez; despues todo funciona sin internet) ---
echo [4/4] Descargando el modelo de voz a models\ (~480 MB)...
set "HF_HOME=%CD%\models"
".venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
if errorlevel 1 goto :error

echo.
echo ============================================================
echo   Listo. Abre la app con doble clic en iniciar.bat
echo ============================================================
pause
exit /b 0

:error
echo.
echo Hubo un error durante la instalacion. Revisa tu conexion a internet
echo y vuelve a ejecutar instalar.bat (continua donde se quedo).
pause
exit /b 1
