@echo off
setlocal
cd /d "%~dp0"
title Transcriptor Local - NO CERRAR mientras grabas

if not exist ".venv\Scripts\python.exe" (
    echo La app no esta instalada. Ejecuta primero instalar.bat
    pause
    exit /b 1
)

echo ============================================================
echo   Transcriptor Local
echo   Se abrira el navegador en http://127.0.0.1:7860
echo.
echo   Minimiza esta ventana, NO la cierres: si la cierras se
echo   detiene la app. Para salir: cierra esta ventana.
echo ============================================================
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
