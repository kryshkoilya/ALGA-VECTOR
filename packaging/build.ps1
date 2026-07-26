param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$SkipPortable,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = if ($PythonPath) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PythonPath)
}
elseif ($env:ALGA_VECTOR_PYTHON) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        $env:ALGA_VECTOR_PYTHON
    )
}
else {
    Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$IsccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$BuildTemp = Join-Path $ProjectRoot ".build-temp"
$ProcessTemp = Join-Path $BuildTemp "system-temp"
$ProcessLocalAppData = Join-Path $BuildTemp "local-app-data"
$PreviousTemp = $env:TEMP
$PreviousTmp = $env:TMP
$PreviousLocalAppData = $env:LOCALAPPDATA
$PreviousPlatformDirsLocalAppData = $env:WIN_PD_OVERRIDE_LOCAL_APPDATA

function Invoke-WindowedSmoke {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $Process = Start-Process `
        -FilePath $Executable `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "$Label failed with exit code $($Process.ExitCode)."
    }
}

function Assert-ExecutableVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedVersion
    )

    $Parts = $ExpectedVersion.Split(".")
    $InvalidParts = @($Parts | Where-Object { $_ -notmatch "^\d+$" })
    if ($Parts.Count -ne 3 -or $InvalidParts.Count -gt 0) {
        throw "Release version must contain exactly three numeric parts: $ExpectedVersion"
    }
    $ExpectedRaw = [Version]::new(
        [int]$Parts[0],
        [int]$Parts[1],
        [int]$Parts[2],
        0
    )
    $VersionInfo = (Get-Item -LiteralPath $Executable).VersionInfo
    if ($VersionInfo.FileVersionRaw -ne $ExpectedRaw) {
        throw (
            "Executable fixed FileVersion mismatch for $Executable. " +
            "Expected $ExpectedRaw, received $($VersionInfo.FileVersionRaw)."
        )
    }
    if ($VersionInfo.ProductVersionRaw -ne $ExpectedRaw) {
        throw (
            "Executable fixed ProductVersion mismatch for $Executable. " +
            "Expected $ExpectedRaw, received $($VersionInfo.ProductVersionRaw)."
        )
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing .venv. Create it and install -e .[dev,hardware] before building."
}

Push-Location $ProjectRoot
try {
    New-Item -ItemType Directory -Path $BuildTemp -Force | Out-Null
    New-Item -ItemType Directory -Path $ProcessTemp -Force | Out-Null
    New-Item -ItemType Directory -Path $ProcessLocalAppData -Force | Out-Null
    $env:TEMP = $ProcessTemp
    $env:TMP = $ProcessTemp
    # Source and frozen smokes must not consume or arm a developer/user profile.
    # Current platformdirs resolves Known Folders through Win32 rather than the
    # plain LOCALAPPDATA variable, so use its explicit Windows test override.
    $env:LOCALAPPDATA = $ProcessLocalAppData
    $env:WIN_PD_OVERRIDE_LOCAL_APPDATA = $ProcessLocalAppData

    $ReleaseVersion = (
        & $Python -c "from alga_vector import __version__; print(__version__)"
    ).Trim()
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $ProjectVersion = (
        & $Python -c (
            "import pathlib,tomllib; " +
            "p=tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8')); " +
            "print(p['project']['version'])"
        )
    ).Trim()
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($ProjectVersion -ne $ReleaseVersion) {
        throw (
            "Version mismatch: package=$ReleaseVersion, " +
            "pyproject=$ProjectVersion."
        )
    }
    $ReleaseCode = ($ReleaseVersion.Split(".") -join "")
    $ReleaseNotesPath = Join-Path `
        $ProjectRoot "docs\RELEASE_${ReleaseCode}_RU.md"
    if (-not (Test-Path -LiteralPath $ReleaseNotesPath -PathType Leaf)) {
        throw "Missing release notes for $ReleaseVersion`: $ReleaseNotesPath"
    }
    $InstallerText = Get-Content `
        -LiteralPath (Join-Path $ProjectRoot "packaging\installer.iss") `
        -Raw
    $InstallerVersionMatch = [regex]::Match(
        $InstallerText,
        '#define\s+MyAppVersion\s+"([^"]+)"'
    )
    if (
        -not $InstallerVersionMatch.Success -or
        $InstallerVersionMatch.Groups[1].Value -ne $ReleaseVersion
    ) {
        throw "Installer version does not match package version $ReleaseVersion."
    }
    $QuickStartHeader = Get-Content `
        -LiteralPath (Join-Path $ProjectRoot "docs\QUICK_START_RU.txt") `
        -TotalCount 1
    if ($QuickStartHeader -notmatch [regex]::Escape($ReleaseVersion)) {
        throw "Quick-start header does not identify release $ReleaseVersion."
    }
    $ReleaseHeader = Get-Content `
        -LiteralPath $ReleaseNotesPath `
        -TotalCount 1
    if ($ReleaseHeader -notmatch [regex]::Escape($ReleaseVersion)) {
        throw "Release-notes header does not identify release $ReleaseVersion."
    }
    $PythonArchitecture = (
        & $Python -c (
            "import platform,struct; " +
            "print(f'{platform.machine()}:{struct.calcsize(chr(80)) * 8}')"
        )
    ).Trim()
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($PythonArchitecture -notin @("AMD64:64", "x86_64:64")) {
        throw (
            "Windows-x64 artifact requires a 64-bit AMD64 Python; " +
            "received $PythonArchitecture."
        )
    }

    if (-not $SkipTests) {
        & $Python -m ruff check src tests
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m mypy src\alga_vector
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m pytest --basetemp .build-temp\pytest
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m alga_vector --hardware-preflight
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m alga_vector --headless-smoke --skip-onboarding `
            --data-dir .build-temp\source-default-live-smoke
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m alga_vector --live --headless-smoke --skip-onboarding `
            --data-dir .build-temp\source-explicit-live-smoke
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m alga_vector --safe --headless-smoke --skip-onboarding `
            --data-dir .build-temp\source-safe-smoke
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m alga_vector --demo --headless-smoke --skip-onboarding `
            --data-dir .build-temp\source-smoke
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $FrozenOutputRoot = Join-Path $ProjectRoot "dist\ALGA VECTOR"
    if (Test-Path -LiteralPath $FrozenOutputRoot) {
        $RunningFrozenProcesses = @(
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.ExecutablePath -and
                    $_.ExecutablePath.StartsWith(
                        $FrozenOutputRoot,
                        [System.StringComparison]::OrdinalIgnoreCase
                    )
                }
        )
        if ($RunningFrozenProcesses.Count -gt 0) {
            $ProcessIds = (
                $RunningFrozenProcesses |
                    ForEach-Object { [string]$_.ProcessId }
            ) -join ", "
            throw (
                "Close the existing ALGA VECTOR build before packaging. " +
                "Running process id(s): $ProcessIds"
            )
        }
    }

    & $Python -m PyInstaller --noconfirm --clean packaging\alga_vector.spec
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $BuiltExe = Join-Path $ProjectRoot "dist\ALGA VECTOR\ALGA VECTOR.exe"
    if (-not (Test-Path -LiteralPath $BuiltExe)) {
        throw "PyInstaller finished without the expected executable: $BuiltExe"
    }
    $CliExe = Join-Path $ProjectRoot "dist\ALGA VECTOR\ALGA VECTOR CLI.exe"
    if (-not (Test-Path -LiteralPath $CliExe)) {
        throw "PyInstaller finished without the expected CLI executable: $CliExe"
    }
    Assert-ExecutableVersion -Executable $BuiltExe -ExpectedVersion $ReleaseVersion
    Assert-ExecutableVersion -Executable $CliExe -ExpectedVersion $ReleaseVersion
    Copy-Item `
        -LiteralPath (Join-Path $ProjectRoot "docs\QUICK_START_RU.txt") `
        -Destination (Join-Path $ProjectRoot "dist\ALGA VECTOR\README_FIRST_RU.txt") `
        -Force
    Copy-Item `
        -LiteralPath $ReleaseNotesPath `
        -Destination (Join-Path $ProjectRoot "dist\ALGA VECTOR\RELEASE_NOTES_RU.md") `
        -Force
    Copy-Item `
        -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") `
        -Destination (Join-Path $ProjectRoot "dist\ALGA VECTOR\THIRD_PARTY_NOTICES.md") `
        -Force

    & $CliExe --hardware-preflight
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $CliExe --headless-smoke --skip-onboarding `
        --data-dir .build-temp\frozen-default-live-smoke
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $CliExe --live --headless-smoke --skip-onboarding `
        --data-dir .build-temp\frozen-explicit-live-smoke
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $CliExe --safe --headless-smoke --skip-onboarding `
        --data-dir .build-temp\frozen-safe-smoke
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $CliExe --demo --headless-smoke --skip-onboarding `
        --data-dir .build-temp\frozen-smoke
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # The GUI executable uses a different Windows subsystem/bootloader than
    # the console executable, so verify it independently and wait explicitly.
    Invoke-WindowedSmoke `
        -Executable $BuiltExe `
        -Label "Frozen GUI default-live smoke" `
        -Arguments @(
            "--headless-smoke",
            "--skip-onboarding",
            "--data-dir",
            ".build-temp\frozen-gui-default-live-smoke"
        )
    Invoke-WindowedSmoke `
        -Executable $BuiltExe `
        -Label "Frozen GUI safe smoke" `
        -Arguments @(
            "--safe",
            "--headless-smoke",
            "--skip-onboarding",
            "--data-dir",
            ".build-temp\frozen-gui-safe-smoke"
        )

    if (-not $SkipPortable) {
        $PortableName = "ALGA_VECTOR-$ReleaseVersion-Windows-x64-onedir"
        $PortableZip = Join-Path $ProjectRoot "dist\$PortableName.zip"
        $PortableChecksum = "$PortableZip.sha256.txt"
        if (
            (Test-Path -LiteralPath $PortableZip) -or
            (Test-Path -LiteralPath $PortableChecksum)
        ) {
            throw (
                "Portable release already exists. Move the previous verified " +
                "artifact before rebuilding: $PortableZip"
            )
        }

        $PortableStageRoot = Join-Path (
            Join-Path $BuildTemp "portable-stage"
        ) ([Guid]::NewGuid().ToString("N"))
        $PortableStageApp = Join-Path $PortableStageRoot "ALGA VECTOR"
        $PortableCandidate = Join-Path `
            $PortableStageRoot "$PortableName.candidate.zip"
        $PortableCandidateChecksum = Join-Path `
            $PortableStageRoot "$PortableName.candidate.zip.sha256.txt"
        New-Item -ItemType Directory -Path $PortableStageRoot -Force | Out-Null
        Copy-Item `
            -LiteralPath (Join-Path $ProjectRoot "dist\ALGA VECTOR") `
            -Destination $PortableStageApp `
            -Recurse
        Compress-Archive `
            -LiteralPath $PortableStageApp `
            -DestinationPath $PortableCandidate `
            -CompressionLevel Optimal

        $PortableQaRoot = Join-Path (
            Join-Path $BuildTemp "portable-qa"
        ) ([Guid]::NewGuid().ToString("N"))
        Expand-Archive `
            -LiteralPath $PortableCandidate `
            -DestinationPath $PortableQaRoot
        $PortableCli = Join-Path $PortableQaRoot "ALGA VECTOR\ALGA VECTOR CLI.exe"
        if (-not (Test-Path -LiteralPath $PortableCli)) {
            throw "Portable archive is missing the CLI executable."
        }
        & $PortableCli --hardware-preflight
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $PortableCli --safe --headless-smoke --skip-onboarding `
            --data-dir .build-temp\portable-safe-smoke
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        # Publish only after extract-and-run QA.  A failed portable smoke must
        # never leave a checksum-bearing release artifact in dist.
        $Hash = Get-FileHash `
            -Algorithm SHA256 `
            -LiteralPath $PortableCandidate
        $ChecksumLine = (
            "$($Hash.Hash)  $([IO.Path]::GetFileName($PortableZip))"
        )
        [IO.File]::WriteAllText(
            $PortableCandidateChecksum,
            "$ChecksumLine`r`n",
            [Text.Encoding]::ASCII
        )
        Move-Item `
            -LiteralPath $PortableCandidate `
            -Destination $PortableZip
        Move-Item `
            -LiteralPath $PortableCandidateChecksum `
            -Destination $PortableChecksum
    }

    if (-not $SkipInstaller) {
        $Iscc = $IsccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $Iscc) {
            throw "Inno Setup 6 not found. Install it or pass -SkipInstaller."
        }
        & $Iscc "packaging\installer.iss"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
finally {
    $env:TEMP = $PreviousTemp
    $env:TMP = $PreviousTmp
    $env:LOCALAPPDATA = $PreviousLocalAppData
    $env:WIN_PD_OVERRIDE_LOCAL_APPDATA = $PreviousPlatformDirsLocalAppData
    Pop-Location
}
