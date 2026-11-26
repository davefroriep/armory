Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
py = "C:\Python314\python.exe"
script = dir & "\nightly_voice_audit.py"
logFile = "S:\ArmoryCam\logs\morning_voice_report.log"
cmd = "cmd.exe /c """"" & py & """ """ & script & """ --deliver-pending >> """ & logFile & """ 2>&1"""
sh.Run cmd, 0, False
