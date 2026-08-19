Option Explicit

Dim fso, shell, scriptDir, skillRoot, ps1, command, exitCode

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
skillRoot = fso.GetParentFolderName(scriptDir)
ps1 = fso.BuildPath(scriptDir, "run_listener.ps1")

shell.CurrentDirectory = skillRoot
command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Quote(ps1)
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Function Quote(value)
  Quote = Chr(34) & value & Chr(34)
End Function
