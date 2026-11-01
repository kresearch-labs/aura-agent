# Aura installer - Windows (Windows PowerShell 5.1 and PowerShell 7).
#
#   irm https://github.com/kresearch-labs/aura-agent/releases/latest/download/install.ps1 | iex
#                                                                       the newest release, then `aura setup`
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1            from the Aura folder it sits in
#   ... install.ps1 -From C:\path\aura_agent-0.2.0-py3-none-any.whl          from a downloaded package
#   ... install.ps1 -Version 0.2.0                                           a given release
#   ... install.ps1 -DryRun     show what would happen; change nothing
#   ... install.ps1 -Yes        don't ask (scripts, CI)
#
# Same steps as install.sh: uv pinned and SHA-256 checked against the value
# written below ->
# %USERPROFILE%\.aura\bin; a private Python (uv-managed); Aura as a uv tool;
# the `aura` command in %USERPROFILE%\.local\bin; `aura --version` must run;
# install.json records the layout. From a release, Aura's package is checked
# against the release's SHA256SUMS before anything is installed - a mismatch
# installs nothing. No admin rights; nothing outside those folders changes
# unless you say yes to adding the command folder to your user PATH.
# Log: %USERPROFILE%\.aura\install.log
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$NoSetup,
    [string]$From = "",
    [string]$Version = "",
    [string]$Extras = "vault",
    [string]$Python = "3.12"
)
$ErrorActionPreference = "Stop"
$UvVersion = "0.12.24"
$Sums = @{                      # published SHA-256 of each uv $UvVersion archive
    "x86_64-pc-windows-msvc"  = "7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019"
    "aarch64-pc-windows-msvc" = "4b783bda5cc44bbae0651a837223873a7acee31152381aef5fc86ef6bef25997"
}
$AuraRepo = "kresearch-labs/aura-agent"
$AuraTag = ""                   # written into the copy attached to a release (vX.Y.Z); empty = the newest

$AuraHome = if ($env:AURA_HOME) { $env:AURA_HOME } else { Join-Path $env:USERPROFILE ".aura" }
$BinDir = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }
$Log = Join-Path $AuraHome "install.log"
$script:Step = 0
$Steps = 5
# piped (irm | iex) there is no script file: stopping must not close the window
$FromFile = [bool]$PSCommandPath
$Interactive = [Environment]::UserInteractive -and -not $Yes -and [Console]::In -ne $null -and -not [Console]::IsInputRedirected

$script:Started = $false          # nothing is written before you say go
function Say([string]$t) {
    Write-Host $t
    if ($script:Started) { try { Add-Content -Path $Log -Value $t -Encoding UTF8 } catch { } }
}
function Native([string]$exe, [string[]]$argv) {
    # Windows PowerShell 5.1: a native program's stderr, redirected while
    # errors stop the script, ends the script even on success - so not here
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $exe @argv *>> $Log } finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}
function Step([string]$t) { $script:Step++; Say ""; Say "[$($script:Step)/$Steps] $t" }
function Stop-Install([int]$code) { throw "AURA_STOP:$code" }
function Card([string]$what, [string]$why = "", [string]$fix = "") {
    Say ""
    Say "x $what"
    if ($why) { Say "  why : $why" }
    if ($fix) { Say "  fix : $fix" }
    if ($script:Started) { Say "  log : $Log" }
    Stop-Install 1
}
function Get-Text([string]$url) {
    # a release's text files come as bytes (application/octet-stream)
    $r = Invoke-WebRequest -Uri $url -UseBasicParsing
    if ($r.Content -is [byte[]]) { return [Text.Encoding]::UTF8.GetString($r.Content) }
    return [string]$r.Content
}
function Get-AuraFolder([string]$dir) {
    # a folder counts only if it really is Aura (not any project's pyproject)
    if (-not $dir) { return $null }
    $pp = Join-Path $dir "pyproject.toml"
    if ((Test-Path $pp) -and (Select-String -Quiet -Path $pp -Pattern '^name = "aura-agent"')) {
        return (Resolve-Path $dir).Path
    }
    return $null
}

function Invoke-AuraInstall {
    # ------------------------------------------------------------ checks --
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    switch ($arch) {
        "AMD64" { $Target = "x86_64-pc-windows-msvc" }
        "ARM64" { $Target = "aarch64-pc-windows-msvc" }
        default { Card "this processor ($arch) has no uv build" "uv ships x86_64 and ARM64" "install Python 3.11+ yourself, then: pip install aura-agent" }
    }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

    # where Aura comes from: -From (a folder or a package), the Aura folder
    # this script sits in, or a release (checked against its SHA256SUMS)
    $Kind = ""; $Src = $null; $Wheel = $null; $Base = $null; $WheelSum = $null; $AuraVer = ""
    if ($From) {
        if ($From -match '\.whl$') {
            if (-not (Test-Path $From)) { Card "no package at $From" "" "check the path" }
            $Kind = "wheel"; $Wheel = (Resolve-Path $From).Path
        } else {
            $Src = Get-AuraFolder $From
            if (-not $Src) { Card "no Aura at $From" "the folder has no Aura pyproject.toml" "give the unzipped Aura folder, or a .whl package" }
            $Kind = "folder"
        }
    } elseif ($PSScriptRoot -and ($Src = Get-AuraFolder (Join-Path $PSScriptRoot ".."))) {
        $Kind = "folder"
    } else {
        $Kind = "release"
        if ($env:AURA_RELEASE_URL) { $Base = $env:AURA_RELEASE_URL.TrimEnd('/') }
        elseif ($Version) { $Base = "https://github.com/$AuraRepo/releases/download/v$($Version.TrimStart('v'))" }
        elseif ($AuraTag) { $Base = "https://github.com/$AuraRepo/releases/download/$AuraTag" }
        else { $Base = "https://github.com/$AuraRepo/releases/latest/download" }
        try { $list = Get-Text "$Base/SHA256SUMS" }
        catch { Card "couldn't read the release" "$Base/SHA256SUMS" "check the connection (a proxy? set HTTPS_PROXY) and the version, then run again" }
        $hits = @(foreach ($line in ($list -split "`n")) {
            if ($line -match '^([0-9a-f]{64})\s+\*?(aura_agent-[0-9][^\s]*-py3-none-any\.whl)\s*$') {
                [pscustomobject]@{ Sum = $Matches[1]; Name = $Matches[2] }
            }
        })
        if ($hits.Count -ne 1) { Card "the release's SHA256SUMS doesn't name exactly one Aura package" "$Base/SHA256SUMS" "this release looks broken - try another version" }
        $WheelSum = $hits[0].Sum; $Wheel = $hits[0].Name
        $AuraVer = ($Wheel -replace '^aura_agent-', '' -replace '-py3-none-any\.whl$', '')
    }
    $Uv = Join-Path $AuraHome "bin\uv.exe"
    $HaveUv = $false
    if (Test-Path $Uv) {
        try { $HaveUv = ((& $Uv --version) -match " $([regex]::Escape($UvVersion))") } catch { $HaveUv = $false }
    }

    Say "Aura installer - Windows $arch"
    switch ($Kind) {
        "folder"  { Say "  from      : $Src" }
        "wheel"   { Say "  from      : $Wheel" }
        "release" { Say "  from      : the release $AuraVer - $Base"; Say "              (its package is checked against SHA256SUMS before it is installed)" }
    }
    Say "  into      : $AuraHome   (program: bin\ python\ tools\ cache\ releases\ - your data stays apart)"
    Say "  command   : $BinDir\aura.exe"
    Say ("  uv        : $UvVersion ($Target)" + $(if ($HaveUv) { " - already here" } else { ", about 20 MB, SHA-256 checked" }))
    Say "  Python    : $Python, private to Aura (about 30 MB the first time)"
    Say "  extras    : $(if ($Extras) { $Extras } else { 'none' })   (web browser: pick it in setup - the installed Edge needs no download)"
    if ($DryRun) { Say ""; Say "dry run - nothing was installed or changed"; Stop-Install 0 }
    if ($Interactive) {
        $ans = Read-Host "Go ahead? [Y/n]"
        if ($ans -match '^(n|no)$') { Write-Host "nothing changed."; Stop-Install 1 }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $AuraHome "bin") | Out-Null
    Set-Content -Path $Log -Value "install started $(Get-Date -Format o)" -Encoding UTF8
    $script:Started = $true

    # ---------------------------------------------------------------- uv --
    Step "uv $UvVersion"
    if ($HaveUv) {
        Say "  already here: $Uv"
    } else {
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("aura-uv-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$Target.zip"
            $zip = Join-Path $tmp "uv.zip"
            try { Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing }
            catch { Card "couldn't download uv" $url "check the connection (a proxy? set HTTPS_PROXY), then run the installer again" }
            $got = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
            if ($got -ne $Sums[$Target]) {
                Card "the uv download doesn't match its published checksum" "expected $($Sums[$Target]), got $got - it may be damaged or not the real file" "run again; if it repeats, don't use this network"
            }
            Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp "x") -Force
            $found = Get-ChildItem -Path (Join-Path $tmp "x") -Recurse -Filter "uv.exe" | Select-Object -First 1
            if (-not $found) { Card "uv wasn't in the archive" "" "run again" }
            Copy-Item $found.FullName $Uv -Force
            Say "  checked (SHA-256) and placed: $Uv"
        } finally {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        }
    }

    # --------------------------------------------------- python + aura --
    Step "Python $Python and Aura"
    if ($Kind -eq "release") {
        # kept in releases\<version>\ - `aura update` can go back to it
        $keep = Join-Path $AuraHome "releases\$AuraVer"
        New-Item -ItemType Directory -Force -Path $keep | Out-Null
        $dest = Join-Path $keep $Wheel
        try { Invoke-WebRequest -Uri "$Base/$Wheel" -OutFile $dest -UseBasicParsing }
        catch { Card "couldn't download Aura's package" "$Base/$Wheel" "check the connection, then run again" }
        $got = (Get-FileHash -Algorithm SHA256 -Path $dest).Hash.ToLower()
        if ($got -ne $WheelSum) {
            Remove-Item -Force $dest -ErrorAction SilentlyContinue
            Card "Aura's package doesn't match the release's checksum" "expected $WheelSum, got $got - it may be damaged or not the real file" "nothing was installed; run again, and if it repeats, don't use this network"
        }
        Say "  checked (SHA-256): $Wheel"
        $Wheel = $dest
    }
    $env:UV_TOOL_DIR = Join-Path $AuraHome "tools"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $AuraHome "python"
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_CACHE_DIR = Join-Path $AuraHome "cache"
    $env:UV_TOOL_BIN_DIR = $BinDir
    # a file:/// address: forward slashes, spaces encoded ("My Projects")
    $path = if ($Kind -eq "folder") { $Src } else { $Wheel }
    $url = "file:///" + (($path -replace '\\', '/') -replace '%', '%25' -replace ' ', '%20')
    $spec = if ($Extras) { "aura-agent[$Extras] @ $url" } else { "aura-agent @ $url" }
    $code = Native $Uv @("tool", "install", "--force", "--managed-python", "--python", $Python, $spec)
    if ($code -ne 0) { Card "Aura didn't install" "see the log's last lines" "Get-Content '$Log' -Tail 30" }
    Say "  installed"

    # -------------------------------------------------------------- PATH --
    Step "the aura command"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $onPath = (($env:Path + ";" + $userPath) -split ";") -contains $BinDir
    if ($onPath) {
        Say "  $BinDir is on PATH"
    } else {
        Say "  $BinDir isn't on your PATH yet"
        $add = $Yes
        if ($Interactive) { $add = -not ((Read-Host "  add it to your user PATH? [Y/n]") -match '^(n|no)$') }
        if ($add) {
            if ((Native $Uv @("tool", "update-shell")) -eq 0) { Say "  added - open a new terminal for it to work" }
            else { Say "  ! couldn't add it - add $BinDir to PATH yourself" }
        } else {
            Say "  use the full path for now: $BinDir\aura.exe"
        }
    }

    # ------------------------------------------------------------ verify --
    Step "check"
    $aura = Join-Path $BinDir "aura.exe"
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $ver = (& $aura --version 2>$null) } catch { $ver = $null } finally { $ErrorActionPreference = $old }
    if (-not $ver) { Card "the aura command doesn't run" "" "Get-Content '$Log' -Tail 30" }
    Say "  $ver runs"
    $layout = [ordered]@{
        method = "uv-tool"; kind = $Kind; source = $(if ($Kind -eq "folder") { $Src } else { $Wheel })
        release_base = $Base; version = $AuraVer; repo = $AuraRepo
        uv = $Uv; uv_version = $UvVersion; python = $Python
        extras = $Extras; bin_dir = $BinDir; tool_dir = $env:UV_TOOL_DIR
        python_dir = $env:UV_PYTHON_INSTALL_DIR; cache_dir = $env:UV_CACHE_DIR
        at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    $layout | ConvertTo-Json | Set-Content -Path (Join-Path $AuraHome "install.json") -Encoding UTF8
    Say "  layout saved: $AuraHome\install.json (aura update / aura uninstall read it)"

    # ------------------------------------------------------------- setup --
    Step "setup"
    if (-not $NoSetup -and $Interactive) {
        & $aura setup
        if ($LASTEXITCODE -ne 0) { Say "  setup stopped - say 'aura setup' any time" }
    } else {
        Say "  next: aura setup        (or: aura setup --answers FILE --non-interactive)"
    }
    Say ""
    Say "done. to update, ask Aura (or: aura update) - remove: aura uninstall (your data stays unless --purge)"
}

try {
    Invoke-AuraInstall
} catch {
    $m = [string]$_.Exception.Message
    if ($m -notmatch '^AURA_STOP:(\d+)$') { throw }
    if ($FromFile) { exit [int]$Matches[1] }
}
