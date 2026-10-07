param([switch]$SkipDependencies)
$ErrorActionPreference = 'Stop'
$workspaceDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExe = Join-Path $workspaceDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & python -m venv (Join-Path $workspaceDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}
if (-not $SkipDependencies) {
    & $pythonExe -m pip install -r (Join-Path $workspaceDir 'requirements-validation.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$compilerDir = Join-Path $workspaceDir 'tools\roslyn-4.14.0'
if (-not (Test-Path -LiteralPath (Join-Path $compilerDir 'tasks\net472\csc.exe'))) {
    [void][IO.Directory]::CreateDirectory((Join-Path $workspaceDir 'tools'))
    $archivePath = Join-Path $workspaceDir 'tools\roslyn-4.14.0.zip'
    Invoke-WebRequest -Uri 'https://www.nuget.org/api/v2/package/Microsoft.Net.Compilers.Toolset/4.14.0' -OutFile $archivePath
    Expand-Archive -LiteralPath $archivePath -DestinationPath $compilerDir -Force
}
& (Join-Path $PSScriptRoot 'build-tool.ps1')
& (Join-Path $PSScriptRoot 'probe-solidworks.ps1') -OutputPath (Join-Path $workspaceDir 'validation\setup-probe.txt')
Write-Output 'Native tool and MCP environment are ready.'
