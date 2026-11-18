@echo off
setlocal
set "PATH=C:\Users\davef\Tools\ffmpeg\bin;%PATH%"
set "ARMORY_CAM_FFMPEG=C:\Users\davef\Tools\ffmpeg\bin\ffmpeg.exe"
set "ARMORY_CAM_FFPROBE=C:\Users\davef\Tools\ffmpeg\bin\ffprobe.exe"
"C:\Python314\python.exe" "%~dp0armory_cam_server.py"
