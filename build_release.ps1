[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.0.0',

    [ValidatePattern('^\d+\.\d+$')]
    [string]$PythonVersion = '3.13'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$projectPrefix = $projectRoot.TrimEnd('\') + '\'

function Get-SafeProjectPath {
    param([Parameter(Mandatory)][string]$ChildPath)

    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $ChildPath))
    if (-not $fullPath.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use a path outside the project: $fullPath"
    }
    return $fullPath
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter()][string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Find-InnoCompiler {
    $command = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $projectRoot '.tools\inno-setup-7\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw 'Inno Setup 7 was not found. Install JRSoftware.InnoSetup.7 before building.'
}

$buildVenv = Get-SafeProjectPath '.build-venv'
$buildDir = Get-SafeProjectPath 'build'
$distDir = Get-SafeProjectPath 'dist'
$releaseDir = Get-SafeProjectPath 'release'
$stagingDir = Get-SafeProjectPath '.release-staging'

foreach ($target in @($buildDir, $distDir, $releaseDir, $stagingDir)) {
    if (Test-Path -LiteralPath $target) {
        $resolved = (Resolve-Path -LiteralPath $target).Path
        if (-not $resolved.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a path outside the project: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $buildVenv 'Scripts\python.exe'))) {
    Invoke-NativeCommand -FilePath 'py' -Arguments @("-$PythonVersion", '-m', 'venv', $buildVenv)
}

$buildPython = Join-Path $buildVenv 'Scripts\python.exe'
Invoke-NativeCommand -FilePath $buildPython -Arguments @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r',
    (Join-Path $projectRoot 'requirements-build.txt')
)

New-Item -ItemType Directory -Path $stagingDir | Out-Null
$versionParts = $Version.Split('.')
$versionTemplate = Get-Content -Raw -Encoding UTF8 -LiteralPath (
    Join-Path $projectRoot 'packaging\windows_version_info.template'
)
$versionInfo = $versionTemplate.Replace('@VERSION@', $Version)
$versionInfo = $versionInfo.Replace('@MAJOR@', $versionParts[0])
$versionInfo = $versionInfo.Replace('@MINOR@', $versionParts[1])
$versionInfo = $versionInfo.Replace('@PATCH@', $versionParts[2])
Set-Content -LiteralPath (Join-Path $stagingDir 'windows_version_info.txt') `
    -Value $versionInfo -Encoding UTF8

$pyinstaller = Join-Path $buildVenv 'Scripts\pyinstaller.exe'
Invoke-NativeCommand -FilePath $pyinstaller -Arguments @(
    '--noconfirm', '--clean',
    '--distpath', $distDir,
    '--workpath', $buildDir,
    (Join-Path $projectRoot 'nd2_roi_mapper.spec')
)

$distApp = Join-Path $distDir 'ND2 ROI Mapper'
if (-not (Test-Path -LiteralPath (Join-Path $distApp 'ND2 ROI Mapper.exe'))) {
    throw "PyInstaller output is missing: $distApp"
}

New-Item -ItemType Directory -Path $releaseDir | Out-Null
$portableName = "ND2-ROI-Mapper-v$Version"
$portableRoot = Join-Path $stagingDir $portableName
Copy-Item -LiteralPath $distApp -Destination $portableRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $portableRoot
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $portableRoot

$portableZip = Join-Path $releaseDir "ND2-ROI-Mapper-Portable-v$Version.zip"
Compress-Archive -LiteralPath $portableRoot -DestinationPath $portableZip `
    -CompressionLevel Optimal

$innoCompiler = Find-InnoCompiler
Invoke-NativeCommand -FilePath $innoCompiler -Arguments @(
    "/DMyAppVersion=$Version",
    "/DMySourceDir=$portableRoot",
    "/DMyOutputDir=$releaseDir",
    (Join-Path $projectRoot 'installer\nd2_roi_mapper.iss')
)

$setupExe = Join-Path $releaseDir "ND2-ROI-Mapper-Setup-v$Version.exe"
foreach ($artifact in @($portableZip, $setupExe)) {
    if (-not (Test-Path -LiteralPath $artifact)) {
        throw "Release artifact is missing: $artifact"
    }
}

Write-Host ''
Write-Host 'Release artifacts created:' -ForegroundColor Green
Write-Host $setupExe
Write-Host $portableZip
