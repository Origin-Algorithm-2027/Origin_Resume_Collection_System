$ErrorActionPreference = "Stop"

Write-Host "[1/4] Checking Python..."
py --version

Write-Host "[2/4] Creating virtual environment..."
py -m venv .venv

Write-Host "[3/4] Installing Python packages..."
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host "[4/4] Python dependencies installed."
Write-Host ""
Write-Host "For old .doc files, install LibreOffice:"
Write-Host "  https://www.libreoffice.org/download/download-libreoffice/"
Write-Host ""
Write-Host "Then:"
Write-Host "  Copy-Item .env.example .env"
Write-Host "  .\.venv\Scripts\python.exe resume_pipeline.py --check"
