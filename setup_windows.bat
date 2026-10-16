<# : Batch section - runs the PowerShell section below (this file is both)
@echo off
setlocal
title HL7 Relay
set "HL7_SETUP_DIR=%~dp0"
set "HL7_SETUP_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-Expression ([IO.File]::ReadAllText($env:HL7_SETUP_SELF))"
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
#>

# HL7 Relay - one-click setup for Windows.
#
# Installs everything the app needs (Python 3.9+ and Git), fetches the app if
# this file is run on its own, then launches it. Installs are per-user where
# possible, so administrator rights are usually not needed.
#
# Set HL7_RELAY_HOME to choose where a standalone install goes
# (default: %LOCALAPPDATA%\HL7Relay).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$RepoUrl = 'https://github.com/Aakvatech-Limited/hl7.git'
$RepoBranch = 'version-15-hotfix'
$PythonVersion = '3.12.7'
$ScriptDir = "$env:HL7_SETUP_DIR".TrimEnd('\')
$InstallDir = if ($env:HL7_RELAY_HOME) { $env:HL7_RELAY_HOME } else { Join-Path $env:LOCALAPPDATA 'HL7Relay' }

function Step($msg) { Write-Host "[...] $msg" }
function Ok($msg)   { Write-Host "[OK]  $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "[X]   $msg" -ForegroundColor Red; exit 1 }

Write-Host '============================================'
Write-Host '  HL7 Relay'
Write-Host '============================================'
Write-Host ''
Write-Host 'Starting automatic setup and launch...'
Write-Host ''

$Arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }

function Update-SessionPath {
    # Pick up PATH changes made by installers without reopening the window
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Get-File($url, $name) {
    $dest = Join-Path $env:TEMP $name
    Step "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    return $dest
}

function Test-Winget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }

# --------------------------------------------------------------------------- #
#  Python
# --------------------------------------------------------------------------- #
function Find-Python {
    # Returns the full path of a working Python 3.9+, or $null
    $candidates = @()
    foreach ($name in 'py', 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        # Skip the Microsoft Store "python" stub, which only opens the Store
        if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') { $candidates += $cmd.Source }
    }
    $candidates += Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                                       "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
                   Sort-Object FullName -Descending | ForEach-Object { $_.FullName }

    foreach ($exe in $candidates) {
        $prefix = if ((Split-Path $exe -Leaf) -eq 'py.exe') { @('-3') } else { @() }
        try {
            $out = & $exe @prefix -c 'import sys, venv; print(sys.executable) if sys.version_info >= (3, 9) else sys.exit(1)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
        } catch {}
    }
    return $null
}

function Install-Python {
    if (Test-Winget) {
        Step 'Installing Python with winget...'
        & winget install --id Python.Python.3.12 -e --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        Update-SessionPath
        if (Find-Python) { return }
        Step 'winget did not finish the install; downloading from python.org instead...'
    }
    $suffix = switch ($Arch) { 'AMD64' { '-amd64' } 'ARM64' { '-arm64' } default { '' } }
    $installer = Get-File "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion$suffix.exe" 'python-setup.exe'
    Step 'Installing Python (this may take a minute)...'
    $proc = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList `
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_pip=1',
        'Include_launcher=1', 'InstallLauncherAllUsers=0', 'Include_test=0'
    Remove-Item $installer -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { Fail "Python installer failed (exit code $($proc.ExitCode))." }
    Update-SessionPath
}

# --------------------------------------------------------------------------- #
#  Git
# --------------------------------------------------------------------------- #
function Find-Git {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($path in "$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe") {
        if (Test-Path $path) {
            # Installed but not on PATH yet: make it visible to this session and the app
            $env:Path = "$(Split-Path $path);$env:Path"
            return $path
        }
    }
    return $null
}

function Install-Git {
    if (Test-Winget) {
        Step 'Installing Git with winget...'
        & winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
        Update-SessionPath
        if (Find-Git) { return }
        Step 'winget did not finish the install; downloading from GitHub instead...'
    }
    $pattern = switch ($Arch) { 'ARM64' { '^Git-.*-arm64\.exe$' } 'x86' { '^Git-.*-32-bit\.exe$' } default { '^Git-.*-64-bit\.exe$' } }
    $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -UseBasicParsing
    $asset = $release.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
    if (-not $asset) { Fail 'Could not find a Git installer for this computer. Install Git from https://git-scm.com and run this again.' }
    $installer = Get-File $asset.browser_download_url 'git-setup.exe'
    Step 'Installing Git (this may take a minute)...'
    $proc = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList `
        '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES', '/CURRENTUSER'
    Remove-Item $installer -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { Fail "Git installer failed (exit code $($proc.ExitCode))." }
    Update-SessionPath
}

try {
    $Python = Find-Python
    if (-not $Python) {
        Install-Python
        $Python = Find-Python
        if (-not $Python) { Fail 'Python is still not available. Install Python 3.9+ from https://www.python.org/downloads/ and run this again.' }
    }
    Ok "Python: $(& $Python --version) ($Python)"

    $Git = Find-Git
    if (-not $Git) {
        Install-Git
        $Git = Find-Git
        if (-not $Git) { Fail 'Git is still not available. Install Git from https://git-scm.com and run this again.' }
    }
    Ok "Git:    $(& $Git --version)"

    # ----------------------------------------------------------------------- #
    #  App files
    # ----------------------------------------------------------------------- #
    if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir 'run.py'))) {
        $AppDir = $ScriptDir
    } elseif (Test-Path (Join-Path $InstallDir 'run.py')) {
        $AppDir = $InstallDir
        Ok "Using existing install in $AppDir"
    } else {
        if ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force | Select-Object -First 1)) {
            Fail "$InstallDir exists but is not a HL7 Relay install. Remove it or set HL7_RELAY_HOME to another folder."
        }
        Step "Downloading HL7 Relay into $InstallDir..."
        & $Git clone --branch $RepoBranch $RepoUrl $InstallDir
        if ($LASTEXITCODE -ne 0) { Fail 'Downloading the app failed. Check your internet connection and try again.' }
        $AppDir = $InstallDir
        Ok 'Downloaded.'
    }
} catch {
    Fail "Setup failed: $($_.Exception.Message)"
}

# --------------------------------------------------------------------------- #
#  Launch (run.py creates the venv and installs pip dependencies)
# --------------------------------------------------------------------------- #
Set-Location $AppDir
& $Python (Join-Path $AppDir 'run.py')
exit $LASTEXITCODE
