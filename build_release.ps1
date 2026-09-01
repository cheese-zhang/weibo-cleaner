$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Push-Location $root
try {

py -3 -m pip install -r (Join-Path $root "requirements.txt") pyinstaller
py -3 -m PyInstaller --noconfirm --clean --onedir --windowed --name "微博清理工具" --collect-all playwright --add-data "$root\weibo_ui.html;." (Join-Path $root "weibo_app.py")

Write-Host "完成：$(Join-Path $root 'dist\微博清理工具')"
}
finally {
    Pop-Location
}
