# Build and optionally ADB-install the headless Fire TV Cube Audio Mixer APK.
#   .\cube-audio\build_cube_audio_apk.ps1
#   .\cube-audio\build_cube_audio_apk.ps1 -Install -Serial 192.168.1.xxx:5555
param(
  [switch]$Install,
  [string]$Serial = ''
)
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$env:JAVA_HOME = 'C:\Users\davef\Tools\jdk-21'
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
$javaBin = "$env:JAVA_HOME\bin"
$sdk = "$env:LOCALAPPDATA\Android\Sdk"
$androidJar = "$sdk\platforms\android-34\android.jar"
$bt = "$sdk\build-tools\36.0.0"

$aapt2 = "$bt\aapt2.exe"
$d8 = "$bt\d8.bat"
$zipalign = "$bt\zipalign.exe"
$apksigner = "$bt\apksigner.bat"
$javac = "$javaBin\javac.exe"
$jar = "$javaBin\jar.exe"
$adb = "$sdk\platform-tools\adb.exe"
if (-not (Test-Path $adb)) { $adb = 'adb' }

$keystore = "$env:USERPROFILE\.android\debug.keystore"
if (-not (Test-Path $keystore)) { throw "Missing debug keystore at $keystore" }

$buildDir = Join-Path $root 'build'
$classesDir = Join-Path $buildDir 'classes'
$dexDir = Join-Path $buildDir 'dex'
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
New-Item -ItemType Directory -Force -Path $classesDir, $dexDir | Out-Null

$unsignedApk = Join-Path $buildDir 'unsigned.apk'
$alignedApk = Join-Path $buildDir 'aligned.apk'
$outApk = Join-Path $buildDir 'armory-cube-audio.apk'

& $aapt2 link -o $unsignedApk -I $androidJar --manifest (Join-Path $root 'AndroidManifest.xml') --min-sdk-version 24 --target-sdk-version 28
if ($LASTEXITCODE) { throw 'aapt2 link failed' }

$javaSrcs = Get-ChildItem -Recurse -Filter '*.java' (Join-Path $root 'src') | Select-Object -ExpandProperty FullName
& $javac --release 8 -cp $androidJar -d $classesDir @javaSrcs
if ($LASTEXITCODE) { throw 'javac failed' }

$classFiles = Get-ChildItem -Recurse -Filter '*.class' $classesDir | Select-Object -ExpandProperty FullName
& $d8 --min-api 24 --lib $androidJar --output $dexDir @classFiles
if ($LASTEXITCODE) { throw 'd8 failed' }

Push-Location $dexDir
try {
  & $jar uf $unsignedApk 'classes.dex'
  if ($LASTEXITCODE) { throw 'jar uf failed' }
} finally {
  Pop-Location
}

& $zipalign -f 4 $unsignedApk $alignedApk
if ($LASTEXITCODE) { throw 'zipalign failed' }

& $apksigner sign --ks $keystore --ks-key-alias androiddebugkey --ks-pass pass:android --key-pass pass:android --out $outApk $alignedApk
if ($LASTEXITCODE) { throw 'apksigner failed' }

Write-Host "APK: $outApk"

if ($Install) {
  $adbArgs = @()
  if ($Serial) { $adbArgs += @('-s', $Serial) }
  & $adb @adbArgs install -r $outApk
  if ($LASTEXITCODE) { throw 'adb install failed' }
  & $adb @adbArgs shell dumpsys deviceidle whitelist +com.froriep.armoryaudio
  & $adb @adbArgs shell am start-foreground-service -n com.froriep.armoryaudio/.ArmoryAudioService
}
