
param([string]$Hora = "08:30")
$ErrorActionPreference = "Stop"

$TaskName = "LicitaFacil - Ingesta diaria"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$App = Join-Path $Repo "app"
$Script = Join-Path $PSScriptRoot "ingesta_diaria.ps1"

if ($Hora -notmatch '^([01]?\d|2[0-3]):[0-5]\d$') {
    Write-Host "La hora debe tener el formato HH:MM, por ejemplo 08:30. No se instalo nada."
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $App ".env"))) {
    Write-Host "Falta app\.env con DATABASE_URL (y GEMINI_API_KEY para la lectura de bases). No se instalo nada."
    exit 1
}

# Ruta real de Python (no el alias de la Microsoft Store, que no sirve en una tarea programada)
$Python = ""
try { $Python = (& python -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1) } catch { }
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
    Write-Host "No se encontro Python en esta terminal. Active el entorno de la app y vuelva a ejecutar el instalador."
    exit 1
}
& $Python -c "import flask, sqlalchemy, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "El Python $Python no tiene las dependencias de la app (pip install -r app\requirements.txt)."
    exit 1
}
if (-not (Get-Command tesseract -ErrorAction SilentlyContinue) -or -not (Get-Command pdftoppm -ErrorAction SilentlyContinue)) {
    Write-Host "Aviso: no se encontraron tesseract y pdftoppm. La ingesta funciona igual, pero las bases escaneadas"
    Write-Host "(PDF de imagenes) quedaran como 'no legibles' porque no se les puede aplicar OCR."
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Python `"$Python`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $App
$trigger = New-ScheduledTaskTrigger -Daily -At $Hora
# StartWhenAvailable: si la PC estaba apagada a esa hora, corre apenas se encienda.
# WakeToRun: si esta en reposo, la despierta (si el plan de energia permite los temporizadores de activacion).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Ingesta diaria de convocatorias del OECE (LicitaFacil)." -Force | Out-Null

Write-Host "Tarea '$TaskName' instalada: todos los dias a las $Hora."
Write-Host "Python: $Python"
Write-Host "Registro: $App\logs\ingesta_<fecha>.log"
Write-Host ""
Write-Host "Probarla ahora:        Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Ver su ultimo resultado: Get-ScheduledTaskInfo -TaskName '$TaskName' | Select-Object LastRunTime, LastTaskResult, NextRunTime"
Write-Host "Ver las ejecuciones:   cd app; python ingest_job.py --ejecuciones"
Write-Host ""
Write-Host "La tarea corre solo con su sesion de Windows iniciada (la pantalla puede estar bloqueada)."