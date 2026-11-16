# Build the Armory Fit Android app (debug APK), optionally install via adb or publish to GitHub Releases.
#   .\scripts\android.ps1            # build only
#   .\scripts\android.ps1 -Install   # build + adb install -r
#   .\scripts\android.ps1 -Publish   # build + attach armory-fit.apk to GitHub Release vX.Y
# Uses the portable JDK 21 in C:\Users\davef\Tools and the Android SDK in %LOCALAPPDATA%\Android\Sdk.
param([switch]$Install, [switch]$NoBump, [switch]$Publish)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

$env:JAVA_HOME = 'C:\Users\davef\Tools\jdk-21'
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
"sdk.dir=" + ($env:ANDROID_HOME -replace '\\', '/') | Set-Content android\local.properties -Encoding ascii

if ($NoBump) { npm run build:ci } else { npm run build }
if ($LASTEXITCODE) { throw 'web build failed' }
npx cap sync android
if ($LASTEXITCODE) { throw 'cap sync failed' }

$buildRoot = ($env:LOCALAPPDATA -replace '\\', '/') + '/armory-fit-build'
Push-Location android
.\gradlew.bat assembleDebug --console=plain "-ParmoryBuildRoot=$buildRoot"
if ($LASTEXITCODE) { Pop-Location; throw 'gradle failed' }
Pop-Location

$builtApk = Join-Path $env:LOCALAPPDATA 'armory-fit-build\app\outputs\apk\debug\app-debug.apk'
$apk = 'android\app\build\outputs\apk\debug\app-debug.apk'
New-Item -ItemType Directory -Force -Path (Split-Path $apk) | Out-Null
Copy-Item -Force $builtApk $apk
Write-Host "APK: $((Resolve-Path $apk).Path)"
if ($Install) { & "$env:ANDROID_HOME\platform-tools\adb.exe" install -r $apk }
if ($Publish) {
  $rev = Get-Content rev.json -Raw | ConvertFrom-Json
  $tag = "v$($rev.major).$($rev.build)"
  $gh = 'C:\Program Files\GitHub CLI\gh.exe'
  $stage = Join-Path ([System.IO.Path]::GetTempPath()) 'armory-fit.apk'
  Copy-Item -Force $apk $stage
  $prevErr = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & $gh release view $tag *> $null
  $exists = ($LASTEXITCODE -eq 0)
  $ErrorActionPreference = $prevErr
  if ($exists) {
    & $gh release upload $tag $stage --clobber
  } else {
    & $gh release create $tag $stage --title "Armory Fit $tag" --notes "Android APK build ($tag)" --latest
  }
  if ($LASTEXITCODE) { throw "gh release publish failed for $tag" }
  Write-Host "Release URL: https://github.com/davefroriep/armory/releases/latest/download/armory-fit.apk"
}
