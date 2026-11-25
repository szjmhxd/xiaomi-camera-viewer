@echo off
chcp 65001 >nul
setlocal
if "%~1"=="" (
  echo 用法: run.bat "录像目录路径"
  echo 示例: run.bat "D:\xiaomi_camera_videos"
  exit /b 1
)
python "%~dp0server.py" --root "%~1" %2 %3 %4 %5
pause
