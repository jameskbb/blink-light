@echo off
rem Long-running runner started at logon by the "BlinkLight Autostart"
rem scheduled task (via blink-light-autostart.vbs, which hides the console).
rem Sleeps until each top of the hour and pulses. Ctrl+C to stop when run by hand.
setlocal
call "%~dp0blink-light.bat" chime run
exit /b %ERRORLEVEL%
