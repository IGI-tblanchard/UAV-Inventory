$ErrorActionPreference = 'Stop'

$propy = 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat'
$scriptRoot = 'C:\Users\tblanchard\Documents\Tracy\Code\UAV Updates\UAV-Inventory'
$reportRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports'
$dataRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive'
$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$transcriptPath = Join-Path $reportRoot "uav_nightly_scheduler_$runStamp.log"
$scripts = @(
    '1_reproject_UAV_LiDAR_nightly.py',
    '1_reproject_UAV_ortho_nightly.py',
    '2_UAV_LiDAR_to_Mosaic_nightly.py',
    '2_UAV_ortho_to_mosaic_nightly.py'
)

$transcriptStarted = $false
$failureCount = 0

try {
    Start-Transcript -LiteralPath $transcriptPath -Force | Out-Null
    $transcriptStarted = $true
    $env:UAV_REPORT_DIR = $reportRoot
    Write-Output "Started UAV nightly workflow: $(Get-Date -Format o)"
    Write-Output "Python launcher: $propy"
    Write-Output "Working folder: $scriptRoot"
    Write-Output "UNC data share check: $(Test-Path -LiteralPath $dataRoot)"

    if (-not (Test-Path -LiteralPath $propy -PathType Leaf)) {
        throw "ArcGIS Pro Python launcher was not found: $propy"
    }
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "UNC data share is unavailable to this scheduled task account: $dataRoot"
    }
    if (-not (Test-Path -LiteralPath $reportRoot -PathType Container)) {
        throw "UAV reports folder is unavailable to this scheduled task account: $reportRoot"
    }

    foreach ($scriptName in $scripts) {
        $scriptPath = Join-Path $scriptRoot $scriptName
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
            Write-Error "Script not found: $scriptPath"
            $failureCount++
            continue
        }

        Write-Output "`n===== START $scriptName : $(Get-Date -Format o) ====="
        & $propy $scriptPath
        $scriptExitCode = $LASTEXITCODE
        Write-Output "===== END $scriptName : exit=$scriptExitCode : $(Get-Date -Format o) ====="
        if ($scriptExitCode -ne 0) {
            $failureCount++
        }
    }

    Write-Output "`nFinished UAV nightly workflow: $(Get-Date -Format o)"
    Write-Output "Scripts with nonzero exit codes or missing files: $failureCount"
    Write-Output "Per-script diagnostics CSV files are saved in: $reportRoot"
}
catch {
    Write-Error (($_ | Out-String).Trim())
    $failureCount++
}
finally {
    Remove-Item Env:\UAV_REPORT_DIR -ErrorAction SilentlyContinue
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
}

if ($failureCount -gt 0) {
    exit 1
}
exit 0
