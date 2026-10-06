param(
    [Parameter(Position = 0)]
    [string]$MosaicDataset = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Staging.gdb\Cenovus_DSM'
)

$ErrorActionPreference = 'Stop'
$propy = 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat'
$scriptRoot = $PSScriptRoot
$testScript = Join-Path $scriptRoot 'test_export_mosaic_paths_once.py'
$reportRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports'

if (-not (Test-Path -LiteralPath $propy -PathType Leaf)) {
    throw "ArcGIS Pro Python launcher was not found: $propy"
}
if (-not (Test-Path -LiteralPath $testScript -PathType Leaf)) {
    throw "Test script was not found: $testScript"
}

$previousLocation = Get-Location
$hadReportDir = Test-Path Env:\UAV_REPORT_DIR
$previousReportDir = $env:UAV_REPORT_DIR
$pythonExitCode = 1

try {
    Set-Location -LiteralPath $scriptRoot
    $env:UAV_REPORT_DIR = $reportRoot
    Write-Output "Python launcher: $propy"
    Write-Output "Working folder: $scriptRoot"
    Write-Output "Mosaic dataset under test: $MosaicDataset"

    & $propy $testScript $MosaicDataset
    $pythonExitCode = $LASTEXITCODE
}
finally {
    Set-Location -LiteralPath $previousLocation
    if ($hadReportDir) {
        $env:UAV_REPORT_DIR = $previousReportDir
    }
    else {
        Remove-Item Env:\UAV_REPORT_DIR -ErrorAction SilentlyContinue
    }
}

exit $pythonExitCode
