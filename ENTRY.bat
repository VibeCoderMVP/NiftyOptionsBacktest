@echo off
:: =============================================================================
:: ENTRY.bat  (NiftyOptionsBacktest)
::
:: Thursday 15:20-15:28 IST — run this after your 6 SELL orders are filled.
::
:: What it does:
::   Logs entry LTPs into data/options_journal.jsonl (the paper trade record).
::   Also backfills entry_ltp into active_options_position.json so the
::   LTP service can show unrealized P&L from the start.
::
:: LTP order (same as the signal order slip):
::   ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE
::
:: Prerequisite: SIGNAL.bat must have been run this Thursday first.
::   (It writes data/.last_signal.json with the ATM.)
::
:: If logging a Thursday trade on a later day (e.g. Friday catch-up),
::   enter the actual Thursday date when prompted.
:: =============================================================================

title Nifty Options Entry Log
color 0A
cls

echo.
echo ================================================================
echo   NIFTY WEEKLY OPTIONS  ^|  LOG ENTRY LTPs
echo   %date% %time%
echo   Run AFTER your 6 SELL orders are filled at 15:20-15:28 IST
echo ================================================================
echo.
echo LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE
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

set /p SPOT=Nifty spot at time of entry:
set /p L1=ATM-50 CE fill price:
set /p L2=ATM-50 PE fill price:
set /p L3=ATM     CE fill price:
set /p L4=ATM     PE fill price:
set /p L5=ATM+50  CE fill price:
set /p L6=ATM+50  PE fill price:

echo.
set ENTRY_DATE_ARG=
set /p ENTRY_DATE=Entry date if not today (YYYY-MM-DD, or press Enter for today):
if not "%ENTRY_DATE%"=="" set ENTRY_DATE_ARG=--entry-date %ENTRY_DATE%

echo.
echo Logging entry...
%UV_CMD% run python pipeline.py paper-entry %ENTRY_DATE_ARG% %SPOT% %L1% %L2% %L3% %L4% %L5% %L6%

echo.
echo Done. Open EasyTerminal F4 tab to see the live position.
echo The LTP service (START_OPTIONS_LTP.bat) must be running to see live updates.
pause
