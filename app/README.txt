Run PowerShell as Admin and paste:
# Compute next top-of-hour start time as HH:MM
$now = Get-Date
$start = (Get-Date -Hour $now.AddHours(1).Hour -Minute 0 -Second 0).ToString("HH:mm")

# Remove any old task
schtasks /Delete /TN "AutoAccountingHourly" /F 2>$null

# Create hourly task starting at the next :00
schtasks /Create /TN "AutoAccountingHourly" /SC HOURLY /MO 1 /ST $start /RL HIGHEST /TR `
"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \"cd `\"$env:USERPROFILE\auto_accounting`\"; & uv run python app/main.py\""