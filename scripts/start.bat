@echo off
setlocal

echo Cleaning old Gaze Mouse processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
	"$targets = @('core\\server_win.py','core\\hud_win.py','core\\calibration_win.py','core\\launcher_win.py');" ^
	"$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.Name -match '^pythonw?\.exe$' };" ^
	"foreach ($p in $procs) {" ^
	"  foreach ($t in $targets) {" ^
	"    if ($p.CommandLine -match [regex]::Escape($t)) {" ^
	"      try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Output ('[start] killed PID ' + $p.ProcessId + ' (' + $t + ')') } catch {}" ^
	"      break" ^
	"    }" ^
	"  }" ^
	"}"

if /I "%~1"=="--clean-only" (
	echo Cleanup complete. Exiting due to --clean-only.
	exit /b 0
)

echo Starting Gaze Mouse System (Windows 11)...
python "%~dp0..\core\launcher_win.py"
pause
