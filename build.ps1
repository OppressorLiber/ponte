param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --distpath 'dist' --workpath 'build' 'Ponte.spec'
    if ($LASTEXITCODE -ne 0) { throw 'O empacotamento falhou.' }
} finally {
    Pop-Location
}

