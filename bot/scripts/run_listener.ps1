param(
  [string]$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [int]$RestartDelaySeconds = 10
)

$ErrorActionPreference = "Stop"

$LogDir = Join-Path $SkillRoot "tmp\service-logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$ProjectRoot = (Resolve-Path (Join-Path $SkillRoot "..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $VenvPython) {
  $Python = $VenvPython
} else {
  $Python = (Get-Command python -ErrorAction Stop).Source
}
$Script = Join-Path $SkillRoot "scripts\feishu_bot.py"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Transcript = Join-Path $LogDir "listener-host-$Stamp.log"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Start-Transcript -Path $Transcript -Append | Out-Null
Write-Host "[$(Get-Date -Format s)] listener host started"
Write-Host "SkillRoot=$SkillRoot"
Write-Host "Python=$Python"
Write-Host "Script=$Script"

function Get-ChildProcessTreeIds {
  param([int]$RootPid)

  $Ids = New-Object System.Collections.Generic.List[int]
  $Queue = New-Object System.Collections.Generic.Queue[int]
  $Queue.Enqueue($RootPid)
  while ($Queue.Count -gt 0) {
    $Id = $Queue.Dequeue()
    $Ids.Add($Id)
    Get-CimInstance Win32_Process |
      Where-Object { $_.ParentProcessId -eq $Id } |
      ForEach-Object { $Queue.Enqueue([int]$_.ProcessId) }
  }
  return @($Ids)
}

function Stop-StaleListenerProcesses {
  try {
    $Processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
  } catch {
    # Process inspection is optional; restricted Windows accounts may deny WMI.
    Write-Host "[$(Get-Date -Format s)] cannot inspect stale processes; continuing without cleanup: $($_.Exception.Message)"
    return
  }
  $Candidates = @(
    $Processes |
      Where-Object {
        $CommandLine = [string]$_.CommandLine
        $IsWrapper = ($_.Name -eq "powershell.exe") -and ($CommandLine -match "-File\s+.*run_listener\.ps1")
        $IsListener = ($_.Name -eq "python.exe") -and ($CommandLine -match "feishu_bot\.py\s+listen")
        $IsEventConsumer = ($_.Name -in @("cmd.exe", "node.exe", "lark-cli.exe")) -and
          ($CommandLine -match "event consume im\.message\.receive_v1")
        ($IsWrapper -or $IsListener -or $IsEventConsumer) -and
        ([int]$_.ProcessId -ne [int]$PID)
      }
  )
  if (-not $Candidates) {
    return
  }

  $CandidateIds = @($Candidates | ForEach-Object { [int]$_.ProcessId })
  $Roots = @($Candidates | Where-Object { $CandidateIds -notcontains [int]$_.ParentProcessId })
  foreach ($Root in $Roots) {
    $Ids = Get-ChildProcessTreeIds -RootPid ([int]$Root.ProcessId)
    Write-Host "[$(Get-Date -Format s)] stopping stale listener root=$($Root.ProcessId) ids=$($Ids -join ',')"
    $Ids |
      Sort-Object -Descending |
      ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  }
}

Stop-StaleListenerProcesses

while ($true) {
  $RunStamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $Stdout = Join-Path $LogDir "listener-$RunStamp.out.log"
  $Stderr = Join-Path $LogDir "listener-$RunStamp.err.log"
  Write-Host "[$(Get-Date -Format s)] starting listener"
  try {
    try {
      $Process = Start-Process `
        -FilePath $Python `
        -ArgumentList @("-X", "utf8", "-u", $Script, "listen") `
        -WorkingDirectory $SkillRoot `
        -RedirectStandardOutput $Stdout `
        -RedirectStandardError $Stderr `
        -WindowStyle Hidden `
        -PassThru
      $Process.WaitForExit()
      $ExitCode = $Process.ExitCode
    } catch {
      # PowerShell 5 can reject Start-Process when PATH/Path differ only by case.
      # Running from this already-hidden host preserves the same log contract.
      Write-Host "[$(Get-Date -Format s)] Start-Process failed; using direct hidden-host fallback: $($_.Exception.Message)"
      $PreviousErrorActionPreference = $ErrorActionPreference
      $ErrorActionPreference = "Continue"
      & $Python -X utf8 -u $Script listen 1> $Stdout 2> $Stderr
      $ExitCode = $LASTEXITCODE
      $ErrorActionPreference = $PreviousErrorActionPreference
    }
  } finally {
  }
  Write-Host "[$(Get-Date -Format s)] listener exited code=$ExitCode"
  Write-Host "stdout=$Stdout"
  Write-Host "stderr=$Stderr"
  Start-Sleep -Seconds $RestartDelaySeconds
}
