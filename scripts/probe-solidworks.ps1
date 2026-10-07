param(
    [switch]$Start,
    [switch]$Inspect,
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\validation\typed-com-probe.txt')
)

# Run the typed, x64, STA probe built by build-tool.ps1. No CAD documents are opened or saved.
$ErrorActionPreference = 'Stop'
$executable = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\build\bin\SolidWorksProbe.exe'))
if (-not (Test-Path -LiteralPath $executable)) { throw "Probe not built: $executable. Run scripts\build-tool.ps1 first." }
$probeArguments = @()
if ($Start) { $probeArguments += '--start' }
if ($Inspect) { $probeArguments += '--inspect' }
$fullOutputPath = [IO.Path]::GetFullPath($OutputPath)
[void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($fullOutputPath))
$probeOutput = & $executable @probeArguments
$probeExitCode = $LASTEXITCODE
[IO.File]::WriteAllLines($fullOutputPath, [string[]]$probeOutput, (New-Object Text.UTF8Encoding($false)))
Write-Output $probeOutput
exit $probeExitCode
