@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title ScrApp.MB
python scrapp.py
if errorlevel 1 pause
