# Builds the backend app into one windowed exe: backend\dist\LLM Dungeon Master.exe
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File backend\build.ps1
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    python -m pip install --quiet -r requirements.txt -r requirements-build.txt
    if (-not $?) { throw "pip install failed" }
    # --windowed: no console. --collect-data sv_ttk: the dark theme's Tcl files.
    # --add-data: the mod, which the app installs into the game (modinstall.py),
    # the icon again so the window can use it too (--icon only sets the exe's), and the DM model whitelist.
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name "LLM Dungeon Master" `
        --icon icon.ico --add-data "icon.ico;." --add-data "model_whitelist.json;." `
        --collect-data sv_ttk --add-data "..\mod\llm_dungeon_master;mod\llm_dungeon_master" app.py
    if (-not $?) { throw "PyInstaller failed" }
    Write-Host "Built: $PSScriptRoot\dist\LLM Dungeon Master.exe"
} finally {
    Pop-Location
}
