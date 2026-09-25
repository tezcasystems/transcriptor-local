@echo off
setlocal
cd /d "%~dp0"
title Instalando Transcriptor Local

echo ============================================================
echo   Instalacion de Transcriptor Local
echo   Tarda unos minutos y necesita internet (~1.5 GB en total).
echo ============================================================
echo.

rem --- 1. Python (3.12 recomendado) ---
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [1/4] Instalando Python 3.12...
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    set "PY=py -3.12"
) else (
    echo [1/4] Python encontrado: %PY%
)

rem --- 2. Entorno y librerias ---
echo [2/4] Instalando librerias...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 goto :error

rem --- 3. ffmpeg (necesario para grabar) ---
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [3/4] Instalando ffmpeg...
    winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements
) else (
    echo [3/4] ffmpeg ya esta instalado.
)

rem --- 4. Modelo de voz (una sola vez; despues todo funciona sin internet) ---
echo [4/4] Descargando el modelo de voz (~480 MB)...
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
