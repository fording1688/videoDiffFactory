Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (!(Test-Path ".venv")) {
  py -3 -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --onefile --name VideoVariantStudio `
  --add-data "static;static" `
  --add-data "runtime;runtime" `
  app\launcher.py
Write-Host "Build done: dist\VideoVariantStudio.exe"
