[CmdletBinding()]
param(
    [ValidateSet('Enable', 'Disable', 'Status')]
    [string] $Mode = 'Status',
    [string] $Workspace,
    [string] $Pythonw,
    [string] $DeliveryLedger
)
$ErrorActionPreference = 'Stop'
$taskName = 'APU Nightly Dias Review'
$description = 'APU manual-work pilot for Dias. One bounded review per local schedule date.'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task -and $task.Description -ne $description) {
    throw "A different task already owns the name $taskName."
}
if ($Mode -eq 'Disable') {
    if ($task) { Disable-ScheduledTask -TaskName $taskName | Out-Null }
}
elseif ($Mode -eq 'Enable') {
    if ((Get-TimeZone).Id -ne 'Eastern Standard Time') {
        throw 'The pilot requires the America/New_York workstation time zone.'
    }
    if (-not $Workspace -or -not $Pythonw -or -not [IO.Path]::IsPathRooted($Workspace) -or
        -not [IO.Path]::IsPathRooted($Pythonw)) {
        throw 'Supply the absolute Dias workspace and APU pythonw.exe paths.'
    }
    $repo = Get-Item -LiteralPath $Workspace
    $python = Get-Item -LiteralPath $Pythonw
    if (-not $repo.PSIsContainer -or $repo.Name -ne 'dias' -or $python.Name -ne 'pythonw.exe') {
        throw 'Expected the Dias directory and the APU environment pythonw.exe.'
    }
    $consolePython = Join-Path $python.DirectoryName 'python.exe'
    & $consolePython -m apu behavior review run --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'This Python environment lacks the APU review command.' }
    $arguments = '-m apu behavior review run --retry-once --cwd "{0}"' -f $repo.FullName
    if ($DeliveryLedger) {
        if (-not [IO.Path]::IsPathRooted($DeliveryLedger) -or
            [IO.Path]::GetFileName($DeliveryLedger) -ne 'apu-deliveries.json' -or $DeliveryLedger.Contains('"')) {
            throw 'Supply an absolute Taskmaster apu-deliveries.json path.'
        }
        $arguments += ' --delivery-ledger "{0}"' -f ([IO.Path]::GetFullPath($DeliveryLedger))
    }
    $action = New-ScheduledTaskAction -Execute $python.FullName -WorkingDirectory $repo.FullName -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(3).AddMinutes(15))
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
    Register-ScheduledTask -TaskName $taskName -Description $description -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{
        TaskName = $taskName
        State = $task.State.ToString()
        NextRun = $info.NextRunTime
        LastRun = $info.LastRunTime
        LastResult = $info.LastTaskResult
        CatchUp = $task.Settings.StartWhenAvailable
        ExtraRetries = [int]($task.Actions.Arguments -match '--retry-once')
    }
} else {
    [pscustomobject]@{TaskName=$taskName; State='Unregistered'}
}
