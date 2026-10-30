$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$dist = Join-Path $root "dist"

& $python -m pip install -q --disable-pip-version-check -r (Join-Path $root "requirements.txt") pyinstaller==6.22.3
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name RoboTrader `
    --distpath $dist `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    --paths $root `
    --add-data "$(Join-Path $root 'robotrader\ui');ui" `
    (Join-Path $root "robotrader\__main__.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Pronto: $(Join-Path $dist 'RoboTrader.exe')"
