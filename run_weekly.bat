@echo off
:: ═══════════════════════════════════════════════════════════════
:: SEA Electronics Newsletter — Weekly Runner
:: ═══════════════════════════════════════════════════════════════
:: Double-click this file to generate the newsletter manually.
:: Or add it to Windows Task Scheduler to run every Friday.
::
:: Task Scheduler setup (run once in PowerShell as Admin):
::   $action  = New-ScheduledTaskAction -Execute "E:\Work\Weekly report\weekly report_claude code\run_weekly.bat"
::   $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 8am
::   Register-ScheduledTask -TaskName "SEA Newsletter" -Action $action -Trigger $trigger -RunLevel Highest
:: ═══════════════════════════════════════════════════════════════

cd /d "%~dp0"

echo.
echo ══ SEA Consumer Electronics Newsletter Generator ══
echo Starting at %DATE% %TIME%
echo.

:: Activate virtual environment if it exists
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    echo [venv] Using .venv virtual environment
) else (
    echo [venv] No .venv found — using system Python
)

:: Run the generator
python generate_newsletter.py

:: Capture exit code
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% EQU 0 (
    echo.
    echo ✓ Newsletter generated successfully!
    echo   Output folder: %OUTPUT_DIR%
) else (
    echo.
    echo ✗ Generation failed with exit code %EXIT_CODE%
    echo   Check newsletter_generator.log for details
)

echo.
echo Finished at %TIME%
echo.

:: Keep window open if run by double-clicking (not from Task Scheduler)
if "%1"=="" pause
