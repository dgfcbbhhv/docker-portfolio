# 数据/笔记/配置备份（Windows PowerShell）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$backupRoot = Join-Path $root "backups"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$target = Join-Path $backupRoot $stamp

New-Item -ItemType Directory -Force -Path $target | Out-Null

$items = @("data", "notes", "config.json", "requirements.txt", "requirements.lock.txt")
foreach ($item in $items) {
    $src = Join-Path $root $item
    if (Test-Path $src) {
        Copy-Item -LiteralPath $src -Destination $target -Recurse -Force
    }
}

# 只保留最近 14 份备份
$keep = 14
$dirs = Get-ChildItem -LiteralPath $backupRoot -Directory |
    Sort-Object Name -Descending | Select-Object -Skip $keep
$backupFull = [System.IO.Path]::GetFullPath($backupRoot)
foreach ($d in $dirs) {
    $full = [System.IO.Path]::GetFullPath($d.FullName)
    if (-not $full.StartsWith($backupFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to delete outside backups dir: $full"
    }
    Remove-Item -LiteralPath $full -Recurse -Force
}

Write-Host "Backup created: $target"
