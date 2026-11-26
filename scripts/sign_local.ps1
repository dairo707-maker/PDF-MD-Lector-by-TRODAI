<#
.SYNOPSIS
    Free ($0) local Authenticode code signing helper for PDF/MD Lector by TRODAI.
    Uses native PowerShell PKI cmdlets (New-SelfSignedCertificate + Set-AuthenticodeSignature)
    with zero Windows SDK dependencies.

.DESCRIPTION
    - Default mode: creates or reuses a local per-user Code Signing certificate in
      Cert:\CurrentUser\My and signs target executables via Set-AuthenticodeSignature.
    - -InstallTrust: exports the public certificate (.cer) and imports it into
      Cert:\CurrentUser\Root ONLY on explicit invocation (never automatically).
    - -UninstallTrust: removes the TRODAI local code signing certificate from
      Cert:\CurrentUser\Root and Cert:\CurrentUser\My.
    - -DryRun: prints planned actions without reading/modifying any certificate store
      or signing any binary on disk.
#>

[CmdletBinding()]
param(
    [string]$TargetPath = "",
    [switch]$InstallTrust,
    [switch]$UninstallTrust,
    [switch]$DryRun,
    [string]$SubjectName = "CN=TRODAI Local Code Signing (PDF/MD Lector)",
    [string]$TimestampServer = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DefaultTargets = @(
    (Join-Path $RepoRoot "dist\PDF-MD-Lector-Portable.exe"),
    (Join-Path $RepoRoot "dist\PDF-MD-Lector-Setup.exe")
)

function Resolve-TargetFiles {
    param([string]$InputPath)
    if (-not [string]::IsNullOrWhiteSpace($InputPath)) {
        if (-not (Test-Path -LiteralPath $InputPath)) {
            throw "Target binary not found: $InputPath"
        }
        return @((Resolve-Path -LiteralPath $InputPath).Path)
    }
    $found = @()
    foreach ($cand in $DefaultTargets) {
        if (Test-Path -LiteralPath $cand) {
            $found += (Resolve-Path -LiteralPath $cand).Path
        }
    }
    return $found
}

if ($DryRun) {
    Write-Output "[DRY-RUN] sign_local.ps1 ($SubjectName)"
    Write-Output "[DRY-RUN] No changes will be made to Cert:\CurrentUser\Root or Cert:\CurrentUser\My."
    if ($UninstallTrust) {
        Write-Output "[DRY-RUN] Would remove certificates matching '$SubjectName' from CurrentUser\Root and CurrentUser\My."
    }
    elseif ($InstallTrust) {
        Write-Output "[DRY-RUN] Would ensure local CodeSigning certificate '$SubjectName' in CurrentUser\My and import public cert into CurrentUser\Root."
    }
    else {
        Write-Output "[DRY-RUN] Would ensure local CodeSigning certificate '$SubjectName' in CurrentUser\My and sign target binaries with SHA256 Authenticode."
    }
    if (-not [string]::IsNullOrWhiteSpace($TargetPath)) {
        Write-Output "[DRY-RUN] TargetPath: $TargetPath"
    }
    else {
        foreach ($cand in $DefaultTargets) {
            Write-Output "[DRY-RUN] Candidate binary: $cand"
        }
    }
    exit 0
}

if ($UninstallTrust) {
    foreach ($storePath in @("Cert:\CurrentUser\Root", "Cert:\CurrentUser\My")) {
        $matches = Get-ChildItem -Path $storePath | Where-Object { $_.Subject -eq $SubjectName }
        foreach ($cert in $matches) {
            Remove-Item -LiteralPath $cert.PSPath -Force
            Write-Output "Removed certificate $($cert.Thumbprint) from $storePath"
        }
    }
    exit 0
}

$existingCert = Get-ChildItem -Path "Cert:\CurrentUser\My" -CodeSigningCert |
    Where-Object { $_.Subject -eq $SubjectName -and $_.NotAfter -gt (Get-Date).AddDays(7) } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1

if ($null -eq $existingCert) {
    $existingCert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $SubjectName `
        -FriendlyName "TRODAI Local Code Signing (PDF/MD Lector)" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -HashAlgorithm SHA256 `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -NotAfter (Get-Date).AddYears(3)
    Write-Output "Created local Code Signing certificate: $($existingCert.Thumbprint)"
}
else {
    Write-Output "Reusing local Code Signing certificate: $($existingCert.Thumbprint)"
}

if ($InstallTrust) {
    $alreadyTrusted = Get-ChildItem -Path "Cert:\CurrentUser\Root" |
        Where-Object { $_.Thumbprint -eq $existingCert.Thumbprint }
    if ($null -eq $alreadyTrusted) {
        $tempCer = Join-Path ([System.IO.Path]::GetTempPath()) ("trodai_local_signing_" + [System.Guid]::NewGuid().ToString("N") + ".cer")
        try {
            Export-Certificate -Cert $existingCert -FilePath $tempCer -Type CERT | Out-Null
            Import-Certificate -FilePath $tempCer -CertStoreLocation "Cert:\CurrentUser\Root" | Out-Null
            Write-Output "Installed public certificate into Cert:\CurrentUser\Root ($($existingCert.Thumbprint))."
        }
        finally {
            if (Test-Path -LiteralPath $tempCer) {
                Remove-Item -LiteralPath $tempCer -Force -ErrorAction SilentlyContinue
            }
        }
    }
    else {
        Write-Output "Certificate already trusted in Cert:\CurrentUser\Root ($($existingCert.Thumbprint))."
    }
    if ([string]::IsNullOrWhiteSpace($TargetPath)) {
        exit 0
    }
}

$targets = Resolve-TargetFiles -InputPath $TargetPath
if ($targets.Count -eq 0) {
    Write-Output "No target executables found to sign (build dist\PDF-MD-Lector-Portable.exe first)."
    exit 0
}

foreach ($binPath in $targets) {
    $signParams = @{
        FilePath      = $binPath
        Certificate   = $existingCert
        HashAlgorithm = "SHA256"
    }
    if (-not [string]::IsNullOrWhiteSpace($TimestampServer)) {
        $signParams["TimestampServer"] = $TimestampServer
    }
    $sig = Set-AuthenticodeSignature @signParams
    Write-Output "Signed $binPath -> Status: $($sig.Status)"
}

exit 0
