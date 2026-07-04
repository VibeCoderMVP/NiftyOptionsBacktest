@echo off
:: =============================================================================
:: START_SCHEDULER.bat  (NiftyOptionsBacktest)
::
:: Starts the standalone auto-pilot scheduler for the weekly Nifty options
:: paper straddle. Runs independent of EasyTerminal -- keep this running
:: continuously (like TW's P1/P2/P3) so Thursday 15:20 auto-entry and
:: expiry-day 15:25 auto-exit fire whether or not ET's TUI is open.
::
:: Leave this running at all times on a trading day. It self-throttles to
:: one action per day per job (entry/exit), so it's safe to just leave open.
:: =============================================================================

title Options Scheduler
color 0B
cls

echo.
echo ================================================================
echo   OPTIONS SCHEDULER  ^|  auto-entry Thu 15:20 / auto-exit expiry 15:25
echo   %date% %time%
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

%UV_CMD% run python scheduler.py
pause
