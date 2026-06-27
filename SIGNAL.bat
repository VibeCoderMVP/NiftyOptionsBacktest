@echo off
:: =============================================================================
:: SIGNAL.bat  (NiftyOptionsBacktest)
::
:: Thursday 15:10-15:30 IST — run this to compute the ATM and order slip.
::
:: What it does:
::   1. Fetches Nifty spot via Dhan REST (auto)
::   2. Computes ATM = round(spot / 50) * 50
::   3. Prints the 6-leg SELL order slip
::   4. Sends Telegram alert (if configured)
::   5. Downloads Dhan instruments CSV and resolves the 6 option security IDs
::   6. Writes D:\Trading\active_options_position.json (read by LTP service + ET)
::   7. Saves ATM/expiry to data/.last_signal.json (read by ENTRY.bat)
::
:: Timing guard: running before 15:10 IST on Thursday shows a warning and exits.
::
:: If Dhan API is unavailable (market closed / weekend testing):
::   Enter the spot price manually when prompted,
::   or pass it on the command line:  SIGNAL.bat 24178
:: =============================================================================

title Nifty Options Signal
color 0A
cls

echo.
echo ================================================================
echo   NIFTY WEEKLY OPTIONS  ^|  THURSDAY SIGNAL
echo   %date% %time%
echo   Run at 15:10-15:30 IST to compute ATM and order slip
echo ================================================================
echo.

cd /d "%~dp0"

set "UV_CMD=uv"
where uv >nul 2>&1
if errorlevel 1 (
    set "UV_CMD=C:\Users\Aditi\AppData\Local\Microsoft\WinGet\Links\uv.exe"
    if not exist "%UV_CMD%" (
        echo ERROR: uv not found.
        pause
        exit /b 1
    )
)

:: Accept optional spot price as command-line arg (e.g. SIGNAL.bat 24178)
if not "%~1"=="" (
    echo Using provided spot: %~1
    %UV_CMD% run python pipeline.py signal --spot %~1
    goto :done
)

%UV_CMD% run python pipeline.py signal

:done
echo.
echo Next step: place your 6 SELL orders, then run ENTRY.bat
echo with the actual LTPs you received.
pause
