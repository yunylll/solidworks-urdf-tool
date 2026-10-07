param([string]$SolidWorksDir, [string]$ExporterDir)
$ErrorActionPreference = 'Stop'
$workspaceDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $SolidWorksDir) { $SolidWorksDir = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\SolidWorks\SOLIDWORKS 2026\Setup').'SolidWorks Folder' }
if (-not $ExporterDir) { $ExporterDir = Join-Path $SolidWorksDir 'URDFExporter' }
$bin = Join-Path $workspaceDir 'build\bin'
[void][IO.Directory]::CreateDirectory($bin)
foreach ($name in @('log4net.dll','MathNet.Numerics.dll','CsvHelper.dll')) { Copy-Item -LiteralPath (Join-Path $ExporterDir $name) -Destination $bin -Force }
foreach ($name in @('sldworks','swconst','swpublished')) { Copy-Item -LiteralPath (Join-Path $SolidWorksDir "api\redist\SolidWorks.Interop.$name.dll") -Destination $bin -Force }
$pythonExe = Join-Path $workspaceDir '.venv\Scripts\python.exe'
& $pythonExe (Join-Path $PSScriptRoot 'prepare-core.py')
if ($LASTEXITCODE -ne 0) { throw 'Core source preparation failed.' }
$compiler = Join-Path $workspaceDir 'tools\roslyn-4.14.0\tasks\net472\csc.exe'
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$references = @('log4net.dll','MathNet.Numerics.dll','CsvHelper.dll') | ForEach-Object { '/reference:' + (Join-Path $bin $_) }
$references += @('SolidWorks.Interop.sldworks.dll','SolidWorks.Interop.swconst.dll','SolidWorks.Interop.swpublished.dll') | ForEach-Object { '/link:' + (Join-Path $bin $_) }
$references += @('System.Windows.Forms.dll','System.Runtime.Serialization.dll','System.Web.Extensions.dll','System.Xml.Linq.dll','Microsoft.CSharp.dll') | ForEach-Object { '/reference:' + (Join-Path $framework $_) }
$references += @('PresentationFramework.dll','WindowsBase.dll','PresentationCore.dll') | ForEach-Object { '/reference:' + (Join-Path (Join-Path $framework 'WPF') $_) }
$vbReference = Get-ChildItem -LiteralPath 'C:\Windows\Microsoft.NET\assembly\GAC_MSIL\Microsoft.VisualBasic' -Recurse -Filter Microsoft.VisualBasic.dll | Select-Object -First 1
$references += '/reference:' + $vbReference.FullName
$sources = Get-ChildItem -LiteralPath (Join-Path $workspaceDir 'build\core-source') -Recurse -Filter '*.cs' | ForEach-Object { $_.FullName }
& $compiler /nologo /target:library /platform:x64 /langversion:latest ('/out:' + (Join-Path $bin 'SW2URDF.dll')) $references $sources
if ($LASTEXITCODE -ne 0) { throw 'Headless core compilation failed.' }
# Keep embedded COM generic types inside one assembly; the external contract is JSON.
& $compiler /nologo /platform:x64 /langversion:latest ('/out:' + (Join-Path $bin 'SolidWorksUrdf.exe')) $references $sources (Join-Path $PSScriptRoot 'LegacyExportBridge.cs') (Join-Path $PSScriptRoot 'ToolOperations.cs')
if ($LASTEXITCODE -ne 0) { throw 'Tool compilation failed.' }
Copy-Item -LiteralPath (Join-Path $workspaceDir 'vendor\solidworks_urdf_exporter\LICENSE') -Destination (Join-Path $bin 'SW2URDF-LICENSE.txt') -Force
& $compiler /nologo /platform:x64 /langversion:latest /main:NestedFixtureBuilder ('/out:' + (Join-Path $bin 'NestedFixtureBuilder.exe')) $references $sources (Join-Path $PSScriptRoot 'LegacyExportBridge.cs') (Join-Path $PSScriptRoot 'ToolOperations.cs') (Join-Path $PSScriptRoot 'FixtureUtilities.cs')
if ($LASTEXITCODE -ne 0) { throw 'Fixture utility compilation failed.' }
# Read-only session probe; tool_service needs it before every CAD job.
& $compiler /nologo /platform:x64 ('/out:' + (Join-Path $bin 'SolidWorksProbe.exe')) ('/reference:' + (Join-Path $bin 'SolidWorks.Interop.sldworks.dll')) ('/reference:' + (Join-Path $bin 'SolidWorks.Interop.swconst.dll')) ('/reference:' + (Join-Path $framework 'System.Web.Extensions.dll')) (Join-Path $PSScriptRoot 'SolidWorksProbe.cs')
if ($LASTEXITCODE -ne 0) { throw 'Probe compilation failed.' }
Write-Output ('Built SolidWorks 2026 headless tool in ' + $bin)
