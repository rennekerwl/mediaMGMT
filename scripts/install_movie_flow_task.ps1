[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateNotNullOrEmpty()]
    [string]$TaskName = "mediaMGMT Movie Flow",

    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 15,

    [switch]$Remove,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Remove) {
    $summary = [ordered]@{
        operation = "remove"
        task_name = $TaskName
        media_and_history_preserved = $true
        dry_run = [bool]$DryRun
    }
    if (-not $DryRun -and $PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    $summary | ConvertTo-Json
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "The project virtual-environment Python executable was not found: $pythonPath"
}

$actionArguments = "-m media_scope.movie_flow run"
$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument $actionArguments `
    -WorkingDirectory $repositoryRoot

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$repetitionTemplate = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 1)
$repeatingTrigger = New-ScheduledTaskTrigger -Daily -At "12:00 AM"
$repeatingTrigger.Repetition = $repetitionTemplate.Repetition

$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$summary = [ordered]@{
    operation = "install"
    task_name = $TaskName
    interval_minutes = $IntervalMinutes
    execute = $pythonPath
    arguments = $actionArguments
    working_directory = $repositoryRoot
    user = $identity
    logon_type = "Interactive"
    triggers = @("AtLogOn", "DailyRepeating")
    repetition_duration_hours = 24
    run_while_logged_out = $false
    dry_run = [bool]$DryRun
}

if (-not $DryRun -and $PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($logonTrigger, $repeatingTrigger) `
        -Principal $principal `
        -Settings $settings `
        -Description "Checkpointed mediaMGMT movie acquisition flow." `
        -Force | Out-Null
}

$summary | ConvertTo-Json
