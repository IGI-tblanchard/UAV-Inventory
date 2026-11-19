param([switch]$ForceMaintenance)

$ErrorActionPreference = 'Stop'

$propy = 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat'
$scriptRoot = 'C:\Users\tblanchard\Documents\Tracy\Code\UAV Updates\UAV-Inventory'
$reportRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports'
$dataRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive'
$clientRoot = Join-Path $dataRoot 'Client'
$geodatabaseRoot = Join-Path $dataRoot 'Geodatabase'
$runId = [guid]::NewGuid().ToString()
$queuePath = Join-Path $reportRoot "uav_ortho_repair_$runId.json"
$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$transcriptPath = Join-Path $reportRoot "uav_nightly_scheduler_${runStamp}_$runId.log"
$repairScript = '1.1_Repair_Single_Band_Rasters.py'
$scripts = @(
    '1_reproject_UAV_LiDAR_nightly.py',
    '1_reproject_UAV_ortho_nightly.py',
    $repairScript,
    '2_UAV_LiDAR_to_Mosaic_nightly.py',
    '2_UAV_ortho_to_mosaic_nightly.py'
)

$environmentNames = @('UAV_REPORT_DIR', 'UAV_REPAIR_RUN_ID', 'UAV_REPAIR_QUEUE', 'UAV_FORCE_MAINTENANCE')
$originalEnvironment = @{}
foreach ($name in $environmentNames) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$transcriptStarted = $false
$failureCount = 0
$mutex = $null
$mutexOwned = $false

function Test-RepairQueue {
    param([string]$Path, [string]$RunId)

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf -ErrorAction Stop)) {
            throw 'Queue file is missing.'
        }
        $json = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop
        $json = $json.TrimStart([char]0xFEFF)
        if (-not $json.TrimStart([char[]]@(' ', "`t", "`r", "`n")).StartsWith('{')) {
            throw 'Queue must be a JSON object.'
        }
        # Windows PowerShell's ConvertFrom-Json accepts some non-JSON syntax,
        # such as single-quoted strings. Check the lexical grammar first.
        $tokenPattern = '\G(?:[ \t\r\n]+|[{}\[\],:]|true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|"(?:[^"\\\x00-\x1F]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*")'
        $tokens = [regex]::Matches($json, $tokenPattern)
        $consumed = if ($tokens.Count) { $tokens[$tokens.Count - 1].Index + $tokens[$tokens.Count - 1].Length } else { 0 }
        if ($consumed -ne $json.Length) {
            throw 'Queue contains invalid JSON tokens.'
        }
        $queue = $json | ConvertFrom-Json -ErrorAction Stop
        if ($queue -isnot [System.Management.Automation.PSCustomObject] -or
            ($queue.version -isnot [int] -and $queue.version -isnot [long]) -or
            $queue.version -ne 1 -or
            $queue.run_id -isnot [string] -or $queue.run_id -cne $RunId -or
            $queue.complete -isnot [bool] -or $queue.complete -ne $true -or
            $queue.items -isnot [array]) {
            throw 'Queue must contain version 1, the current run_id, complete=true, and an items array.'
        }
        return $true
    }
    catch {
        Write-Warning "Repair queue is missing or invalid ($Path): $($_.Exception.Message)"
        return $false
    }
}

try {
    # Global spans Windows logon sessions on this host, not other computers.
    $mutex = New-Object System.Threading.Mutex($false, 'Global\UAVInventoryNightlyWorkflow')
    try {
        $mutexOwned = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        # WaitOne grants ownership when it reports an abandoned mutex.
        $mutexOwned = $true
        Write-Warning 'An abandoned UAV workflow mutex was acquired; review the previous run.'
    }
    if (-not $mutexOwned) {
        throw 'Another UAV nightly workflow is already running on this host.'
    }
    if (-not (Test-Path -LiteralPath $reportRoot -PathType Container)) {
        throw "UAV reports folder is unavailable to this scheduled task account: $reportRoot"
    }
    # Never accept a queue that was present before this run started.
    if (Test-Path -LiteralPath $queuePath -ErrorAction Stop) {
        throw "Refusing to reuse an existing repair queue: $queuePath"
    }
    Start-Transcript -LiteralPath $transcriptPath -Force | Out-Null
    $transcriptStarted = $true
    $env:UAV_REPORT_DIR = $reportRoot
    $env:UAV_REPAIR_RUN_ID = $runId
    $env:UAV_REPAIR_QUEUE = $queuePath
    if ($ForceMaintenance) {
        $env:UAV_FORCE_MAINTENANCE = 'true'
    }
    Write-Output "Started UAV nightly workflow: $(Get-Date -Format o)"
    Write-Output "Python launcher: $propy"
    Write-Output "Working folder: $scriptRoot"
    Write-Output "Repair run ID: $runId"
    Write-Output "Repair queue: $queuePath"
    Write-Output "Force maintenance: $ForceMaintenance; environment override: $env:UAV_FORCE_MAINTENANCE"
    Write-Output "UNC data share check: $(Test-Path -LiteralPath $dataRoot)"

    if (-not (Test-Path -LiteralPath $propy -PathType Leaf)) {
        throw "ArcGIS Pro Python launcher was not found: $propy"
    }
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "UNC data share is unavailable to this scheduled task account: $dataRoot"
    }
    foreach ($requiredRoot in @($clientRoot, $geodatabaseRoot)) {
        if (-not (Test-Path -LiteralPath $requiredRoot -PathType Container)) {
            throw "Required migrated data folder is unavailable: $requiredRoot"
        }
    }
    foreach ($scriptName in $scripts) {
        if ($scriptName -eq $repairScript -and -not (Test-RepairQueue -Path $queuePath -RunId $runId)) {
            Write-Warning "Skipping $repairScript; continuing to the mosaic scripts."
            $failureCount++
            continue
        }

        try {
            $scriptPath = Join-Path $scriptRoot $scriptName
            if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf -ErrorAction Stop)) {
                throw "Script not found: $scriptPath"
            }
            Write-Output "`n===== START $scriptName : $(Get-Date -Format o) ====="
            $LASTEXITCODE = $null
            & $propy $scriptPath
            $scriptExitCode = $LASTEXITCODE
            Write-Output "===== END $scriptName : exit=$scriptExitCode : $(Get-Date -Format o) ====="
            if ($null -eq $scriptExitCode -or $scriptExitCode -ne 0) {
                $failureCount++
            }
        }
        catch {
            Write-Warning "Failed to execute ${scriptName}: $($_.Exception.Message)"
            $failureCount++
        }
    }

    Write-Output "`nFinished UAV nightly workflow: $(Get-Date -Format o)"
    Write-Output "Failed or skipped workflow steps: $failureCount"
    Write-Output "Per-script diagnostics CSV files are saved in: $reportRoot"
}
catch {
    Write-Error (($_ | Out-String).Trim()) -ErrorAction Continue
    $failureCount++
}
finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
    }
    if ($transcriptStarted) {
        try {
            Stop-Transcript | Out-Null
        }
        catch {
            Write-Warning "Could not stop transcript: $($_.Exception.Message)"
            $failureCount++
        }
    }
    if ($mutexOwned) {
        try {
            $mutex.ReleaseMutex()
        }
        catch {
            Write-Warning "Could not release UAV workflow mutex: $($_.Exception.Message)"
            $failureCount++
        }
    }
    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
}

if ($failureCount -gt 0) {
    exit 1
}
exit 0
