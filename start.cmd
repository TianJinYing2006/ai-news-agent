@echo off
rem 双击运行：一键启动本地服务（自动准备环境并打开浏览器）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
