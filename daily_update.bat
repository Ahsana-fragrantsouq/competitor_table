@echo off
REM ============================================================================================
REM  Daily + weekly competitor update - runs daily_update.py (Windows Task Scheduler starts this Monday-Friday at 1 PM)
REM
REM  1. Put this file + daily_update.py in your scraper folder, next to bp_scraper.py, es_scraper.py,
REM     aj_scraper.py, ng_scraper.py, on_scraper.py (+ alinjaz.py, nichegallerie.py, ounass.py they use)
REM  2. Fill in the two lines marked >>> below (same values as on Render -> Environment)
REM  3. Create the task ONCE (Monday-Friday 1 PM) - open Command Prompt and paste (change the folder if yours is different):
REM       schtasks /Create /TN "Competitor daily update" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 13:00 /TR "\"C:\ff-scraper\daily_update.bat\"" /F
REM     Test it now:   schtasks /Run /TN "Competitor daily update"
REM     Remove it:     schtasks /Delete /TN "Competitor daily update" /F
REM  Results: daily_update.log (+ daily_update_console.txt for errors) in this folder, and Settings -> Automate on the website.
REM ============================================================================================

REM ---- step 1: go to the folder of this file (daily_update.py + the scrapers are here)
cd /d "%~dp0"
echo [%date% %time%] [BAT] Folder: %cd% >> daily_update_console.txt

REM >>> database (EXTERNAL url, ending in /french_fragrance_db)
set "FF_DATABASE_URL=postgresql://...oregon-postgres.render.com/french_fragrance_db"
REM >>> same RUN_SECRET as on Render (lets the script start /competitor/update-least at the end)
set "RUN_SECRET=..."

REM ---- step 2: check the two values above were filled in (the script cannot work without them)
if "%FF_DATABASE_URL:...=%"=="%FF_DATABASE_URL%" (
    echo [%date% %time%] [BAT] FF_DATABASE_URL is set >> daily_update_console.txt
) else (
    echo [%date% %time%] [BAT] ERROR: FF_DATABASE_URL still contains "..." - fill it in this file >> daily_update_console.txt
)
if "%RUN_SECRET%"=="..." echo [%date% %time%] [BAT] WARNING: RUN_SECRET not filled in - Render jobs will not start >> daily_update_console.txt

REM ---- step 3: check Python is installed and on the PATH
where python >nul 2>&1 || echo [%date% %time%] [BAT] ERROR: python not found - install Python or add it to PATH >> daily_update_console.txt

REM ---- step 4: run the update. daily_update.py writes its own log (daily_update.log);
REM      everything it prints + Python errors / crashes also go to daily_update_console.txt
REM      %* = extra options when started by hand, e.g.  daily_update.bat --test
echo [%date% %time%] [BAT] Daily competitor update starting (options: %*) >> daily_update_console.txt
python daily_update.py %* >> daily_update_console.txt 2>&1
echo [%date% %time%] [BAT] Finished (exit code %errorlevel% - 0 = OK) >> daily_update_console.txt