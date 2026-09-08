' Launches blink-light-chime.bat with no console window.
' The scheduled task created by "blink-light.bat chime install" runs:
'   wscript.exe "<repo>\blink-light-chime.vbs"
Option Explicit

Dim fso, shell, scriptDir, target
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = fso.BuildPath(scriptDir, "blink-light-chime.bat")

shell.CurrentDirectory = scriptDir
' 0 = hidden window, True = wait so the task reports the real exit code.
WScript.Quit shell.Run("""" & target & """", 0, True)
