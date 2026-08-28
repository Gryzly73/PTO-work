[CmdletBinding()]
param(
    [ValidateSet("build", "verify", "audit")]
    [string]$Action = "verify",
    [string]$ConverterImage = "pto-dwgtools:local",
    [string]$BackendImage = "pto-backend:dwg-harness"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Test-Image([string]$Image) {
    & docker image inspect $Image *> $null
    return $LASTEXITCODE -eq 0
}

function Build-Images {
    if (-not (Test-Image $ConverterImage)) {
        & docker build `
            -f (Join-Path $repo "Dockerfile.dwgtools") `
            -t $ConverterImage `
            $repo
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build image $ConverterImage"
        }
    }

    & docker build `
        --build-arg "DWGTOOLS_IMAGE=$ConverterImage" `
        -t $BackendImage `
        $repo
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build image $BackendImage"
    }
}

function Assert-BackendImage {
    if (-not (Test-Image $BackendImage)) {
        throw "Image $BackendImage is missing. Run: .\scripts\dwg-harness-docker.ps1 build"
    }
}

switch ($Action) {
    "build" {
        Build-Images
        & docker run --rm $BackendImage dwg2dxf --version
        exit $LASTEXITCODE
    }
    "verify" {
        Assert-BackendImage
        & docker run --rm $BackendImage dwg2dxf --version
        exit $LASTEXITCODE
    }
    "audit" {
        Assert-BackendImage
        $output = Join-Path $repo "local_runs"
        New-Item -ItemType Directory -Force -Path $output | Out-Null
        & docker run --rm `
            --mount "type=bind,source=$repo,target=/work" `
            -w /work `
            $BackendImage `
            python -m dwg_symbols audit /work/new_files/dwg --deep `
            --out /work/local_runs/dwg_audit.deep.json
        if ($LASTEXITCODE -ne 0) {
            throw "Deep audit failed"
        }
        Write-Host "Done: $output\dwg_audit.deep.json"
    }
}
