# Quita la tarea programada de la ingesta diaria de esta PC (no toca la base ni los registros).
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\desinstalar.ps1
$TaskName = "LicitaFacil - Ingesta diaria"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tarea '$TaskName' eliminada."
} else {
    Write-Host "La tarea '$TaskName' no estaba instalada."
}