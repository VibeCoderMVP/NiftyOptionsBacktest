@echo off
:: =============================================================================
:: START_OPTIONS_LTP.bat  (NiftyOptionsBacktest)
::
:: Starts the Options LTP polling service.
:: Polls Dhan REST every 15s for the 6 active option contracts.
:: Publishes live LTPs on ZMQ port 5557 -> EasyTerminal Options tab.
::
:: Run this BEFORE or alongside EasyTerminal on any day the market is open
:: and you have an open options position (Fri/Mon/Tue after Thursday entry).
::
:: Stops automatically when the position is closed (after paper-exit).
:: Restart it next Thursday after running SIGNAL.bat.
:: =============================================================================

title Options LTP Service
color 0B
cls

echo.
echo ================================================================
echo   OPTIONS LTP SERVICE  ^|  ZMQ port 5557
echo   %date% %time%
echo   Polling Dhan REST every 15s for active option LTPs
echo   Press Ctrl+C to stop
echo ================================================================
echo.

cd /d "%~dp0"

set "UV_CMD=uv"
where uv >nul 2>&1
if errorlevel 1 (
    set "UV_CMD=C:\Users\Aditi\AppData\Local\Microsoft\WinGet\Links\uv.exe"
    if not exist "%UV_CMD%" (
        echo ERROR: uv not found. Install uv or check PATH.
        pause
        exit /b 1
    )
)

%UV_CMD% run python options_ltp_service.py
pause
