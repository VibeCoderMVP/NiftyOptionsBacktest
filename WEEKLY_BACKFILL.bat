@echo off
:: =============================================================================
:: WEEKLY_BACKFILL.bat  (NiftyOptionsBacktest)
::
:: Tuesday after 15:30 IST — updates the historical DuckDB with this week's data.
::
:: Steps:
::   1. fetch  — downloads any new rolling option data from Dhan (cached, safe to re-run)
::   2. build  — creates a new weekly parquet for the just-closed expiry
::   3. backtest — recomputes all regime P&L stats including this week
::
:: Cross-check: the net P&L from backtest should match your paper-exit P&L
:: for this week (within a few rupees). If they diverge significantly, the
:: rolling data may have coverage gaps for some strikes (see analyse_trades.py).
::
:: Run time: ~2-3 minutes (most is API fetch, parquet build is instant).
:: =============================================================================

title Nifty Options Weekly Backfill
color 0A
cls

echo.
echo ================================================================
echo   NIFTY OPTIONS  ^|  WEEKLY BACKFILL
echo   %date% %time%
echo   Updating historical database with this week's expiry data
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

echo Step 1/3: Fetching latest rolling option data from Dhan...
%UV_CMD% run python pipeline.py fetch
echo.

echo Step 2/3: Building weekly parquet for new expiry...
%UV_CMD% run python pipeline.py build
echo.

echo Step 3/3: Rerunning backtest across all cycles...
%UV_CMD% run python pipeline.py backtest
echo.

echo ================================================================
echo   BACKFILL COMPLETE
echo   Updated summaries in data/backtest_summary*.csv
echo   Cross-check: this week's backtest P^&L should match your
echo   paper-exit P^&L (within coverage gaps for moved strikes).
echo ================================================================
pause
