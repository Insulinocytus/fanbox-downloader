$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m PyInstaller --noconfirm --clean fanbox-downloader.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$package = Join-Path $PSScriptRoot "dist\fanbox-downloader"
Copy-Item "config.example.json" (Join-Path $package "config.json") -Force
Copy-Item "README.md" $package -Force

$release = Join-Path $PSScriptRoot "release"
New-Item -ItemType Directory -Force $release | Out-Null
$zip = Join-Path $release "fanbox-downloader-windows-x64.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$package\*" -DestinationPath $zip

Write-Host ""
Write-Host "Build complete: $zip"
