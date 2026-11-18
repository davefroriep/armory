$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = 'C:\Python314\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

$ffBin = 'C:\Users\davef\Tools\ffmpeg\bin'
if (Test-Path $ffBin) {
  $env:PATH = "$ffBin;$env:PATH"
  $env:ARMORY_CAM_FFMPEG = Join-Path $ffBin 'ffmpeg.exe'
  $env:ARMORY_CAM_FFPROBE = Join-Path $ffBin 'ffprobe.exe'
}

& $py (Join-Path $scriptDir 'armory_cam_server.py')
