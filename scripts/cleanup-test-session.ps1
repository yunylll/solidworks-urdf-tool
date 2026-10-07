param([Parameter(Mandatory=$true)][string]$Manifest)
$ErrorActionPreference = 'Stop'
$validationRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\validation')) + '\'
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
if (-not $manifestPath.StartsWith($validationRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest must be inside workspace validation directory.' }
$session = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$testProcess = Get-Process -Id $session.pid -ErrorAction SilentlyContinue
if ($null -eq $testProcess) { Write-Output 'Test session already exited.'; exit 0 }
$expectedTime = ([DateTimeOffset]$session.started).UtcTicks
if ($testProcess.ProcessName -ne 'SLDWORKS' -or $testProcess.StartTime.ToUniversalTime().Ticks -ne $expectedTime) { throw 'Process identity mismatch; refusing cleanup.' }
Stop-Process -InputObject $testProcess -Force
Wait-Process -InputObject $testProcess -Timeout 15 -ErrorAction SilentlyContinue
Write-Output ('Closed verified test process ' + $session.pid)
