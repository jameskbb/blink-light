' Launches blink-light-autostart.bat with no console window and waits on it.
' The scheduled task created by "blink-light.bat autostart enable" runs:
'   wscript.exe "<repo>\blink-light-autostart.vbs"
'
' Waiting matters: it keeps the task action alive for the life of the chime
' loop, so Task Scheduler's default "do not start a new instance" policy stops
' a second logon from stacking up a second runner.
Option Explicit

Dim fso, shell, scriptDir, target
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = fso.BuildPath(scriptDir, "blink-light-autostart.bat")

shell.CurrentDirectory = scriptDir
' 0 = hidden window, True = wait for the loop to exit.
WScript.Quit shell.Run("""" & target & """", 0, True)
