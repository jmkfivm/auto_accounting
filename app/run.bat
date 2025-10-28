@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "cd $env:USERPROFILE\auto_accounting\app; uv run python main.py"