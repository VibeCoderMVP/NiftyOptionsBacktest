cd /d "%~dp0"
uv run python stress_test_dynamic_subscriptions.py --interval 60 --iterations 0
pause
