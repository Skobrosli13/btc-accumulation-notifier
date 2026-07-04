@echo off
cd /d C:\dev\Investment\btc-accumulation-notifier
.venv\Scripts\python.exe -m scripts.nightly_lab >> logs\nightly_lab.log 2>&1
