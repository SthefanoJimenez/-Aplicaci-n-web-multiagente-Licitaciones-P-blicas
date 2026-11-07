
param(
    [string]$Python = "",
    [string]$Origen = "programada_windows"
)
$ErrorActionPreference = "Continue"

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$App = Join-Path $Repo "app"
$Logs = Join-Path $App "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$Log = Join-Path $Logs ("ingesta_" + (Get-Date -Format "yyyy-MM-dd") + ".log")
$Lock = Join-Path $Logs "ingesta.lock"

function Write-Log([string]$text) {
    Add-Content -Path $Log -Value $text -Encoding UTF8
}

# Python: el que se registro al instalar la tarea; si no, el del entorno virtual o el del sistema
$candidates = @($Python, (Join-Path $Repo ".venv\Scripts\python.exe"), (Join-Path $App ".venv\Scripts\python.exe"))
$found = Get-Command python -ErrorAction SilentlyContinue
if ($found) { $candidates += $found.Source }
$Python = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { $Python = $candidate; break }
}
if (-not $Python) {
    Write-Log ("{0} No se encontro Python. Reinstale la tarea con scripts\windows\instalar.ps1." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    exit 1
}

# Una sola ingesta a la vez (por si alguien la corre a mano mientras corre la programada)
if (Test-Path -LiteralPath $Lock) {
    $age = (Get-Date) - (Get-Item -LiteralPath $Lock -Force).CreationTime
    if ($age.TotalMinutes -lt 180) {
        Write-Log ("{0} Ya hay una ingesta en curso; esta se omite." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
        exit 0
    }
    Remove-Item -LiteralPath $Lock -Recurse -Force    # quedo de una ejecucion que se corto hace mas de 3 horas
}
New-Item -ItemType Directory -Path $Lock | Out-Null

$code = 1
try {
    $env:INGEST_TRIGGER = $Origen
    $env:PYTHONUNBUFFERED = "1"
    $env:PYTHONIOENCODING = "utf-8"          # la salida de Python se escribe en UTF-8 (con sus tildes)
    Write-Log ("===== {0} | inicio | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Python)

    # La PC no entra en reposo mientras dura la ingesta y la lectura de bases (como caffeinate en la iMac)
    $awake = $false
    try {
        Add-Type -Namespace LicitaFacil -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);' -ErrorAction Stop
        [void][LicitaFacil.Power]::SetThreadExecutionState([uint32]"0x80000001")    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        $awake = $true
    } catch { }

    $out = Join-Path $Logs "salida.tmp"
    $err = Join-Path $Logs "errores.tmp"
    $process = Start-Process -FilePath $Python -ArgumentList "ingest_job.py" -WorkingDirectory $App `
        -RedirectStandardOutput $out -RedirectStandardError $err -NoNewWindow -Wait -PassThru
    $code = $process.ExitCode
    foreach ($file in @($out, $err)) {
        if ((Test-Path -LiteralPath $file) -and (Get-Item -LiteralPath $file -Force).Length -gt 0) {
            Write-Log (Get-Content -LiteralPath $file -Raw -Encoding UTF8).TrimEnd()
        }
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
    if ($awake) { [void][LicitaFacil.Power]::SetThreadExecutionState([uint32]"0x80000000") }
    Write-Log ("===== {0} | fin | codigo {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $code)
} finally {
    Remove-Item -LiteralPath $Lock -Recurse -Force -ErrorAction SilentlyContinue
}

# Se guardan 30 dias de registros
Get-ChildItem -LiteralPath $Logs -Filter "ingesta_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue
exit $code