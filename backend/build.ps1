# Builds the backend app into one windowed exe: backend\dist\LLM Dungeon Master.exe
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File backend\build.ps1
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    python -m pip install --quiet -r requirements.txt pyinstaller
    if (-not $?) { throw "pip install failed" }
    # --windowed: no console. --collect-data sv_ttk: the dark theme's Tcl files.
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name "LLM Dungeon Master" `
        --collect-data sv_ttk app.py
    if (-not $?) { throw "PyInstaller failed" }
    Write-Host "Built: $PSScriptRoot\dist\LLM Dungeon Master.exe"
} finally {
    Pop-Location
}
