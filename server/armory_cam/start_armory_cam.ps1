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

# Wait up to 60s on cold boot for S:\ drive / ArmoryCam root mount before entering supervisor loop
$mountDeadline = (Get-Date).AddSeconds(60)
while (-not (Test-Path $logRoot) -and (Get-Date) -lt $mountDeadline) {
  Start-Sleep -Seconds 2
}

$logsDir = Join-Path $logRoot 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$logFile = Join-Path $logsDir 'server.log'
$stdoutLog = Join-Path $logsDir 'stdout.log'

$ErrorActionPreference = 'Continue'
$backoffSec = 3
while ($true) {
  try {
    if ((Test-Path $stdoutLog) -and ((Get-Item $stdoutLog).Length -gt 5242880)) {
      $prevLog = "$stdoutLog.prev"
      if (Test-Path $prevLog) { Remove-Item -Force $prevLog -ErrorAction SilentlyContinue }
      Move-Item -Force $stdoutLog $prevLog -ErrorAction SilentlyContinue
    }
  } catch {}

  $startTime = Get-Date
  $rc = 0
  try {
    & $py $srvScript *>> $stdoutLog
    $rc = $LASTEXITCODE
  } catch {
    $rc = if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }
  }
  if ($rc -eq 0) { break }

  $uptimeSec = ((Get-Date) - $startTime).TotalSeconds
  if ($uptimeSec -ge 60) { $backoffSec = 3 }

  try {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss,fff'
    Add-Content -Path $logFile -Value "$ts [ERROR] armory_cam_server.py exited unexpectedly (code=$rc, uptime=${uptimeSec}s); restarting in ${backoffSec}s..." -Encoding UTF8
  } catch {}

  Start-Sleep -Seconds $backoffSec
  $backoffSec = [Math]::Min(30, $backoffSec * 2)

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
