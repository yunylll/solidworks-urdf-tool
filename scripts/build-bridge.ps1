# Compatibility alias for the old prototype build command.
param([string]$SolidWorksDir, [string]$ExporterDir)
& (Join-Path $PSScriptRoot 'build-tool.ps1') @PSBoundParameters
