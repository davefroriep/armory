Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
py = "C:\Python314\python.exe"
script = dir & "\nightly_voice_audit.py"
logFile = "S:\ArmoryCam\logs\nightly_voice_audit.log"
cmd = "cmd.exe /c """"" & py & """ """ & script & """ --auto-apply >> """ & logFile & """ 2>&1"""
sh.Run cmd, 0, False
