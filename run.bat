@echo off
chcp 65001 >nul
setlocal
rem 不带参数启动：浏览器里选文件夹，下次自动记住
rem 也可以 run.bat "D:\xiaomi_camera_videos" 直接指定目录
python "%~dp0server.py" %*
pause
