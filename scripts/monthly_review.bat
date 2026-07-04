@echo off
cd /d C:\dev\Investment\btc-accumulation-notifier
.venv\Scripts\python.exe -m scripts.monthly_review >> logs\monthly_review.log 2>&1
