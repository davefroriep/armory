Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = dir & "\start_armory_cam.ps1"
shellExe = "powershell.exe"
If sh.Run("cmd.exe /c where pwsh.exe >nul 2>&1", 0, True) = 0 Then
    shellExe = "pwsh.exe"
End If
sh.Run shellExe & " -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & ps1 & """", 0, False
