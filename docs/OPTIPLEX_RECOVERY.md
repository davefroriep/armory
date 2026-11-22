# OptiPlex (`192.168.1.152`) Rebuild & Recovery Runbook

Concise disaster-recovery guide to stand up `ArmoryCamDVR` on a clean Windows machine.

## 1. Prerequisites
- **Python 3.14:** Install Python 3.14 (`C:\Python314\python.exe`), then install pinned packages:
  ```powershell
  C:\Python314\python.exe -m pip install -r server\armory_cam\requirements.txt
  ```
- **FFmpeg & Platform-Tools (`adb.exe`):** Place FFmpeg (`ffmpeg.exe`, `ffprobe.exe`) at `C:\Users\davef\Tools\ffmpeg\bin\` and Android `adb.exe` on `PATH` or `%LOCALAPPDATA%\Android\Sdk\platform-tools\`.
- **GPU Stack (GTX 1650):** `requirements.txt` includes `faster-whisper`, `ctranslate2`, `onnxruntime-gpu`, `kokoro-onnx`, and CUDA runtime wheels (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`).

## 2. `S:\ArmoryCam\` Data & Models
Restore or recreate `S:\ArmoryCam\`:
- **`models\kokoro\`:** `kokoro-v1.0.onnx` (310 MB) + `voices-v1.0.bin` (26 MB).
- **`Whisper` (`base.en`):** Auto-downloads via HuggingFace on first start (`faster-whisper`).
- **Config & Secrets (Drive Backup):** Auto-backed up to `J:\My Drive\Backups\ArmoryFit Backup\OptiPlex\`:
  - `config.json` (camera RTSP/crops, displays, wallboard, voice, houseAudio)
  - `shows.json` (Show Lab timelines, fxHooks, Shelly URLs)
  - `tts_secrets.json` (cloud TTS API keys)
  - `voice_ab_runs.json`, `wallboard_standby.json`
  Copy those JSONs back to `S:\ArmoryCam\` (or use `POST /api/config/restore` once Drive is mounted).
- **`tts_cache\` & `sfx\`:** `tts_cache\` rebuilds on demand; copy any custom `sfx\*.wav` from backup if used.

## 3. Android Signing Keystore (Zero-Uninstall Pixel Updates)
- Restore `J:\My Drive\Backups\ArmoryFit Backup\Signing\debug.keystore` -> `%USERPROFILE%\.android\debug.keystore` before running `scripts\android.ps1` so in-place APK installs on Dave's Pixel keep IndexedDB intact.

## 4. Scheduled Task (`ArmoryCamDVR`)
- Run at logon under `wscript.exe //B "<repo>\server\armory_cam\start_armory_cam_hidden.vbs"`, which launches `start_armory_cam.ps1` (waits for `S:\`, logs stdout/stderr to `S:\ArmoryCam\logs\stdout.log`, restarts on crash).
- Verify live: `http://192.168.1.152:8766/api/ping` and `http://192.168.1.152:8766/api/voice/status` (`asrDevice: "cuda/int8_float16"`).
