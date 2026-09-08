@echo off
rem Runner for the hourly chime. Invoked by the "BlinkLight Hourly Chime"
rem scheduled task (via blink-light-chime.vbs, which hides the console window).
rem Safe to run by hand: it no-ops if this hour's chime already fired.
setlocal
call "%~dp0blink-light.bat" chime now
exit /b %ERRORLEVEL%
