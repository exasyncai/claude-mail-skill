# claude-mail-skill installer for Windows. Fetches a TAGGED release, verifies it against
# SHA256SUMS, unpacks it to ~\.claude-mail-skill\app and installs the Claude Code skill to
# ~\.claude\skills\mail. Uninstall: delete those two folders.
$ErrorActionPreference = "Stop"
$Repo = "exasyncai/claude-mail-skill"
$Version = if ($env:MAILSKILL_VERSION) { $env:MAILSKILL_VERSION } else { "v0.2.0" }
$HomeDir = if ($env:MAILSKILL_HOME) { $env:MAILSKILL_HOME } else { Join-Path $HOME ".claude-mail-skill" }
$Dest = Join-Path $HomeDir "app"
$Archive = "claude-mail-skill-$Version.zip"
$Base = if ($env:MAILSKILL_BASE_URL) { $env:MAILSKILL_BASE_URL } else { "https://github.com/$Repo/releases/download/$Version" }   # mirror or test server

# Python 3.10+: py launcher first, then python on PATH (skips the Store alias that only opens a window)
$PyExe = $null; $PyArgs = @()
foreach ($cand in @(@("py", "-3"), @("python"), @("python3"))) {
    $exe = $cand[0]; $extra = @($cand | Select-Object -Skip 1)
    try {
        $out = & $exe @($extra + @("-c", "import sys; print(sys.version_info >= (3, 10))")) 2>$null
        if ("$out".Trim() -eq "True") { $PyExe = $exe; $PyArgs = $extra; break }
    } catch { Write-Verbose "$exe not usable: $_" }
}
if (-not $PyExe) { throw "Python 3.10 or newer is required. Install it from python.org or the Microsoft Store, then run this again." }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("claude-mail-skill-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Output "Downloading claude-mail-skill $Version ..."
    Invoke-WebRequest -UseBasicParsing -Uri "$Base/$Archive" -OutFile (Join-Path $tmp $Archive)
    Invoke-WebRequest -UseBasicParsing -Uri "$Base/SHA256SUMS" -OutFile (Join-Path $tmp "SHA256SUMS")

    $line = Get-Content (Join-Path $tmp "SHA256SUMS") | Where-Object { $_ -match "\s\*?$([regex]::Escape($Archive))$" } | Select-Object -First 1
    $expected = if ($line) { ($line -split '\s+')[0].ToLower() } else { "" }
    $actual = (Get-FileHash -Algorithm SHA256 (Join-Path $tmp $Archive)).Hash.ToLower()
    if (-not $expected -or $expected -ne $actual) { throw "Checksum mismatch for $Archive - nothing installed." }

    $unpacked = Join-Path $tmp "x"
    Expand-Archive -Path (Join-Path $tmp $Archive) -DestinationPath $unpacked
    $root = Get-ChildItem $unpacked -Directory | Select-Object -First 1
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Copy-Item -Recurse -Force (Join-Path $root.FullName "*") $Dest
    Write-Output "Installed to $Dest (checksum ok)."

    # keychain support (Windows Credential Manager) through the keyring package
    $hasKeyring = $false
    try { & $PyExe @($PyArgs + @("-c", "import keyring")) 2>$null; $hasKeyring = ($LASTEXITCODE -eq 0) } catch { Write-Verbose "keyring probe failed: $_" }
    if (-not $hasKeyring) {
        Write-Output "Installing the keyring package for your user (pip install --user keyring) ..."
        try { & $PyExe @($PyArgs + @("-m", "pip", "install", "--user", "--quiet", "keyring")) } catch { Write-Output "keyring could not be installed; set MAILSKILL_PASSWORD per session instead." }
    }

    # launcher: mailskill.cmd next to the app, plus a line for the profile
    $pyCall = (@($PyExe) + $PyArgs) -join " "
    $cmd = Join-Path $HomeDir "mailskill.cmd"
    Set-Content -Path $cmd -Value "@echo off`r`n$pyCall `"$Dest\mailskill.py`" %*`r`n" -Encoding ASCII
    Write-Output "Command: $cmd"
    Write-Output "Add this line to your PowerShell profile (notepad `$PROFILE) to call it as 'mailskill':"
    Write-Output "  function mailskill { & '$cmd' @args }"

    # Claude Code skill
    $claude = Join-Path $HOME ".claude"
    if ((Test-Path $claude) -or $env:CLAUDE_INSTALL_SKILL) {
        $skill = Join-Path $claude "skills\mail"
        New-Item -ItemType Directory -Force -Path $skill | Out-Null
        Copy-Item -Force (Join-Path $Dest "skill\mail\SKILL.md") (Join-Path $skill "SKILL.md")
        Write-Output "Claude Code skill installed: $skill"
    } else {
        Write-Output "No ~\.claude found; skill not installed. Later: copy $Dest\skill\mail\SKILL.md to ~\.claude\skills\mail\"
    }
    Write-Output "Start with:  mailskill add you@example.com"
}
finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
