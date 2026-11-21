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

$srvScript = Join-Path $scriptDir 'armory_cam_server.py'
$logRoot = if ($env:ARMORY_CAM_ROOT) { $env:ARMORY_CAM_ROOT } else { 'S:\ArmoryCam' }
$logFile = Join-Path $logRoot 'logs\server.log'

$ErrorActionPreference = 'Continue'
while ($true) {
  $rc = 0
  try {
    & $py $srvScript
    $rc = $LASTEXITCODE
  } catch {
    $rc = if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }
  }
  if ($rc -eq 0) { break }
  try {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss,fff'
    Add-Content -Path $logFile -Value "$ts [ERROR] armory_cam_server.py exited unexpectedly with code $rc; supervisor checking restart..." -Encoding UTF8
  } catch {}
  Start-Sleep -Seconds 3
  $portBusy = $false
  try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $iar = $tcp.BeginConnect('127.0.0.1', 8766, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(400, $false) -and $tcp.Connected) {
      $portBusy = $true
    }
    $tcp.Close()
  } catch {}
  if ($portBusy) { break }
}
