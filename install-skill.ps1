# install-skill.ps1 — 把 skill-forge 导出的交付 ZIP 安装到技能目录
# 用法：
#   .\install-skill.ps1 -Zip <交付ZIP路径> -Target <目标技能目录>
#
# 行为（对应 v1.3 修改建议 P2-6 降级版）：
#   1. 校验 ZIP 存在且含 SKILL.md（不是技能包直接拒绝，不写盘）；
#   2. 目标目录已有同名技能 → 先备份为 <目录>.bak-<时间戳>，绝不静默覆盖；
#   3. 解压失败 → 自动回退（删半成品、恢复备份），目标目录回到安装前状态；
#   4. 全程 -WhatIf 干跑：加 -DryRun 只打印将执行的动作，不写盘。
#
# 示例：
#   # 干跑看看会发生什么
#   .\install-skill.ps1 -Zip data\projects\...\exports\01XXX.zip -Target C:\Users\Xinye\.catpaw\skills\meeting-action-items -DryRun
#   # 真装
#   .\install-skill.ps1 -Zip data\projects\...\exports\01XXX.zip -Target C:\Users\Xinye\.catpaw\skills\meeting-action-items

param(
    [Parameter(Mandatory = $true)]
    [string]$Zip,          # 交付 ZIP 路径（导出接口生成）

    [Parameter(Mandatory = $true)]
    [string]$Target,       # 目标技能目录（将创建或替换）

    [switch]$DryRun        # 干跑：只打印动作，不写盘
)

$ErrorActionPreference = "Stop"

# ---------- 前置校验：ZIP 必须是技能包 ----------
if (-not (Test-Path $Zip)) { Write-Error "ZIP 不存在: $Zip" }
$zipItem = Get-Item $Zip
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($zipItem.FullName)
try {
    $hasSkill = $archive.Entries | Where-Object { $_.FullName -eq "SKILL.md" }
    if (-not $hasSkill) {
        Write-Error "ZIP 缺少 SKILL.md，不是技能交付包，拒绝安装: $Zip"
    }
    $entryCount = $archive.Entries.Count
}
finally { $archive.Dispose() }

$targetDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Target)
$existed = Test-Path $targetDir
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "$targetDir.bak-$stamp"

Write-Host "== 安装计划 =="
Write-Host "  ZIP:    $($zipItem.FullName) ($entryCount 个条目)"
Write-Host "  目标:   $targetDir $(if ($existed) { '（已存在，将先备份）' } else { '（新建）' })"
if ($existed) { Write-Host "  备份:   $backup" }
if ($DryRun) { Write-Host "== DryRun：以上为将执行的动作，未写盘 =="; exit 0 }

# ---------- 备份已有技能 ----------
if ($existed) {
    Move-Item -LiteralPath $targetDir -Destination $backup -Confirm:$false
}

# ---------- 解压（失败自动回退） ----------
try {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    Expand-Archive -LiteralPath $zipItem.FullName -DestinationPath $targetDir -Force
    # 校验解压结果
    if (-not (Test-Path (Join-Path $targetDir "SKILL.md"))) {
        throw "解压后缺少 SKILL.md（包损坏？）"
    }
}
catch {
    Write-Warning "安装失败，正在回退: $($_.Exception.Message)"
    # 清掉半成品
    if (Test-Path $targetDir) { Remove-Item -LiteralPath $targetDir -Recurse -Force -Confirm:$false }
    # 恢复备份
    if ($existed -and (Test-Path $backup)) {
        Move-Item -LiteralPath $backup -Destination $targetDir -Confirm:$false
        Write-Host "已回退：原技能恢复至 $targetDir"
    }
    else {
        Write-Host "已回退：目标目录不存在（全新安装失败，无备份需要恢复）"
    }
    exit 1
}

Write-Host "== 安装完成 =="
Write-Host "  技能位于: $targetDir"
if ($existed) {
    Write-Host "  旧版备份: $backup（确认新版可用后可手动删除）"
}
Write-Host "  提示: 若包内 README 标注为未验证候选（candidate），使用前请先人工核验。"
