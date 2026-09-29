param(
    [ValidateSet("amd64", "arm64")][string]$Arch = "amd64"
)
$ErrorActionPreference = "Stop"
$oldOS = $env:GOOS
$oldArch = $env:GOARCH
$oldCGO = $env:CGO_ENABLED
Push-Location $PSScriptRoot
try {
    $env:GOOS = "windows"
    $env:GOARCH = $Arch
    $env:CGO_ENABLED = "1"
    $target = Join-Path "dist" "windows-$Arch"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    go build -trimpath -buildmode=c-shared -o "$target/bps-excel.dll" .
    if ($LASTEXITCODE -ne 0) { throw "Native plugin build failed" }
    Get-FileHash -Algorithm SHA256 -LiteralPath "$target/bps-excel.dll"
} finally {
    $env:GOOS = $oldOS
    $env:GOARCH = $oldArch
    $env:CGO_ENABLED = $oldCGO
    Pop-Location
}
