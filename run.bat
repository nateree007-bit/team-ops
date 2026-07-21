@echo off
cd /d "%~dp0"
"C:\Users\nater\AppData\Local\Programs\Python\Python312-arm64\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
