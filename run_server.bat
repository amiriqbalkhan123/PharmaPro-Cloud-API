@echo off
title PharmaPro Cloud API Server
echo ========================================
echo    PharmaPro Cloud API Server
echo ========================================
echo.

:: Activate virtual environment
call venv\Scripts\activate.bat

:: Check if venv exists
if errorlevel 1 (
    echo [ERROR] Virtual environment not found!
    echo Please run: python -m venv venv
    pause
    exit /b 1
)

:: Set environment variables
set PYTHONPATH=%CD%

:: Run the server
echo [INFO] Starting FastAPI server...
echo [INFO] URL: http://localhost:8000
echo [INFO] Docs: http://localhost:8000/docs
echo.
echo Press CTRL+C to stop the server
echo ========================================
echo.

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

pause