$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

& $python -m pip install -q --disable-pip-version-check -r (Join-Path $root "requirements.txt") pyinstaller==6.22.3
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name RoboTrader `
    --distpath $root `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    --add-data "$(Join-Path $root 'ui');ui" `
    (Join-Path $root "app.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Pronto: $(Join-Path $root 'RoboTrader.exe')"
