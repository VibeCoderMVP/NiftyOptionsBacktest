@echo off
:: =============================================================================
:: EXIT.bat  (NiftyOptionsBacktest)
::
:: Tuesday 15:20-15:28 IST — run this after buying back all 6 legs.
::
:: What it does:
::   Logs exit LTPs, computes gross and net P&L, marks the trade WIN/LOSS.
::   Also marks active_options_position.json as "closed" so the LTP
::   service stops polling and ET shows the final result.
::
:: Timing: strategy mandates active close by 15:25 on expiry Tuesday.
::   Running before 15:20 on expiry day shows a warning (time decay not captured).
::   Running before expiry day shows a warning (early exit).
::
:: LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE
::   (same order as entry — these are the prices you PAID to buy back)
:: =============================================================================

title Nifty Options Exit Log
color 0A
cls

echo.
echo ================================================================
echo   NIFTY WEEKLY OPTIONS  ^|  LOG EXIT LTPs
echo   %date% %time%
echo   Run AFTER buying back all 6 legs at 15:20-15:25 IST on expiry Tuesday
echo ================================================================
echo.
echo LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE
echo   (the prices you paid to close each leg)
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

set /p L1=ATM-50 CE buyback price:
set /p L2=ATM-50 PE buyback price:
set /p L3=ATM     CE buyback price:
set /p L4=ATM     PE buyback price:
set /p L5=ATM+50  CE buyback price:
set /p L6=ATM+50  PE buyback price:

echo.
echo Logging exit and computing P^&L...
%UV_CMD% run python pipeline.py paper-exit %L1% %L2% %L3% %L4% %L5% %L6%

echo.
echo Trade closed. Run WEEKLY_BACKFILL.bat after 15:30 to update the
echo historical database with this week's data.
pause
