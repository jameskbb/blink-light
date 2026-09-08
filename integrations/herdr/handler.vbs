' Runs handler.py with no console window.
'
' Herdr fires event hooks on every agent status change, so without this a
' console would flash on the desktop several times per agent turn. wscript is
' used rather than cscript because wscript never allocates a console at all.
'
' Resolves the repo's .venv, falling back to whatever python is on PATH; the
' handler itself imports nothing outside the standard library, so either works.
Option Explicit

Dim fso, shell, scriptDir, repoRoot, venvPython, exe, handler
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))

venvPython = fso.BuildPath(repoRoot, ".venv\Scripts\python.exe")
If fso.FileExists(venvPython) Then
  exe = venvPython
Else
  exe = "python"
End If

handler = fso.BuildPath(scriptDir, "handler.py")

shell.CurrentDirectory = repoRoot
' 0 = hidden. False = do not wait: Herdr should never block on the light.
shell.Run """" & exe & """ """ & handler & """", 0, False
WScript.Quit 0
