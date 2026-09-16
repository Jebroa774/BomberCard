[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$ReleaseName = 'prototype',

    [Parameter()]
    [string]$KiCadCli = '',

    [Parameter()]
    [switch]$ChecksOnly,

    [Parameter()]
    [switch]$RequireCompleteProcurement,

    [Parameter()]
    [switch]$AllowDirty,

    [Parameter()]
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..\..')).Path
$hardwareDir = Join-Path $repoRoot 'hardware'
$manufacturingDir = Join-Path $repoRoot 'manufacturing'
$buildRoot = Join-Path $manufacturingDir 'build'
$releaseDir = Join-Path $buildRoot $ReleaseName
$schematic = Join-Path $hardwareDir 'PocketLab-Card.kicad_sch'
$board = Join-Path $hardwareDir 'PocketLab-Card.kicad_pcb'

$defaultDnp = @(
    'C109', # Optional VSYS bulk capacitor; fit only after stability testing.
    'C310', # NFC matching options; values are selected from measurements.
    'C311',
    'C312',
    'C403', # Sub-GHz pi-match shunt options; select after RF measurement.
    'C404',
    'C507', # LF resonance/RX trim options; select with the real external coil.
    'C508',
    'C509'
)

function Find-KiCadCli {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "KiCad CLI not found at '$RequestedPath'."
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $command = Get-Command 'kicad-cli' -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\KiCad\10.0\bin\kicad-cli.exe')
    }
    $candidates += @(
        'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe',
        '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
        '/usr/bin/kicad-cli',
        '/usr/local/bin/kicad-cli'
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw 'KiCad 10 kicad-cli was not found. Pass its path with -KiCadCli.'
}

function Invoke-KiCad {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "`n==> $Description" -ForegroundColor Cyan
    & $script:kicadCommand @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (kicad-cli exit code $LASTEXITCODE)."
    }
}

function Test-Truthy {
    param([object]$Value)
    if ($null -eq $Value) { return $false }
    return ([string]$Value).Trim() -match '^(1|true|yes|y|dnp|x)$'
}

function Get-Designators {
    param([object]$Value)

    # KiCad normally exports one reference per BOM row when --group-by is not
    # requested. Splitting also makes this safe if that export default changes
    # or a grouped raw BOM is supplied during pipeline maintenance.
    return @(([string]$Value -split '[,;\s]+') | ForEach-Object {
            $_.Trim()
        } | Where-Object {
            $_
        })
}

function Get-ReferenceKey {
    param([object]$Value)
    return ([string]$Value).Trim().ToUpperInvariant()
}

function Get-ReportIssues {
    param([object]$Report)

    $issues = @()
    if ($Report.PSObject.Properties.Name -contains 'sheets') {
        foreach ($sheet in @($Report.sheets)) {
            if ($sheet.PSObject.Properties.Name -contains 'violations') {
                $issues += @($sheet.violations)
            }
        }
    }
    foreach ($property in @('violations', 'unconnected_items', 'schematic_parity')) {
        if ($Report.PSObject.Properties.Name -contains $property) {
            $issues += @($Report.$property)
        }
    }
    return @($issues)
}

function Write-CsvUtf8 {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Rows,
        [Parameter(Mandatory = $true)][string]$Path
    )

    # Windows PowerShell 5.1 emits UTF-8 with BOM. JLCPCB and spreadsheet tools
    # accept this reliably, including non-ASCII notes.
    if (@($Rows).Count -eq 0) {
        [IO.File]::WriteAllText($Path, '', (New-Object Text.UTF8Encoding($true)))
        return
    }
    $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
}

foreach ($source in @($schematic, $board)) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required source file is missing: $source"
    }
}

$script:kicadCommand = Find-KiCadCli -RequestedPath $KiCadCli
$kicadVersion = (& $script:kicadCommand --version).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Could not determine the KiCad CLI version.'
}
if ($kicadVersion -notmatch '^10\.') {
    throw "This pipeline is pinned to KiCad 10.x; found '$kicadVersion'."
}

$gitCommand = Get-Command 'git' -ErrorAction SilentlyContinue
$gitCommit = 'unknown'
$gitDirty = $true
if ($gitCommand) {
    $gitCommit = (& git -C $repoRoot rev-parse HEAD 2>$null).Trim()
    $gitState = @(& git -C $repoRoot status --porcelain --untracked-files=normal 2>$null)
    $gitDirty = $gitState.Count -gt 0
}

$releaseBlockers = @()
if ($gitDirty -and -not $AllowDirty) {
    $releaseBlockers += 'Git working tree is not clean (use -AllowDirty only for local draft checks).'
}

if (Test-Path -LiteralPath $releaseDir) {
    if (-not $Force) {
        throw "Output '$releaseDir' already exists. Use a new -ReleaseName or pass -Force."
    }

    $resolvedBuildRoot = [IO.Path]::GetFullPath($buildRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $resolvedRelease = [IO.Path]::GetFullPath($releaseDir)
    if (-not $resolvedRelease.StartsWith($resolvedBuildRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove output outside '$resolvedBuildRoot'."
    }
    Remove-Item -LiteralPath $resolvedRelease -Recurse -Force
}

$reportsDir = Join-Path $releaseDir 'reports'
$gerberDir = Join-Path $releaseDir 'gerbers'
$assemblyDir = Join-Path $releaseDir 'assembly'
$sourceDir = Join-Path $releaseDir 'source'
foreach ($directory in @($reportsDir, $gerberDir, $assemblyDir, $sourceDir)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$ercJson = Join-Path $reportsDir 'erc.json'
$drcJson = Join-Path $reportsDir 'drc.json'

Invoke-KiCad -Description 'Electrical Rules Check (ERC)' -Arguments @(
    'sch', 'erc',
    '--output', $ercJson,
    '--format', 'json',
    '--units', 'mm',
    '--severity-all',
    $schematic
)

Invoke-KiCad -Description 'PCB Design Rules Check and schematic parity check (DRC)' -Arguments @(
    'pcb', 'drc',
    '--output', $drcJson,
    '--format', 'json',
    '--units', 'mm',
    '--severity-all',
    '--schematic-parity',
    '--refill-zones',
    $board
)

$ercReport = Get-Content -LiteralPath $ercJson -Raw | ConvertFrom-Json
$drcReport = Get-Content -LiteralPath $drcJson -Raw | ConvertFrom-Json
$ercIssues = @(Get-ReportIssues -Report $ercReport)
$drcIssues = @(Get-ReportIssues -Report $drcReport)
$ercErrors = @($ercIssues | Where-Object { $_.severity -eq 'error' })
$ercWarnings = @($ercIssues | Where-Object { $_.severity -eq 'warning' })
$drcErrors = @($drcIssues | Where-Object { $_.severity -eq 'error' })
$drcWarnings = @($drcIssues | Where-Object { $_.severity -eq 'warning' })
$parityIssues = @($drcReport.schematic_parity)

if ($ercErrors.Count -gt 0) {
    $releaseBlockers += "ERC contains $($ercErrors.Count) error(s)."
}
if ($drcErrors.Count -gt 0) {
    $releaseBlockers += "DRC contains $($drcErrors.Count) error(s)."
}
if ($parityIssues.Count -gt 0) {
    $releaseBlockers += "PCB/schematic parity contains $($parityIssues.Count) mismatch(es)."
}

$boardText = [IO.File]::ReadAllText($board)
if ($boardText -match '(?i)PLACEMENT-DRAFT|UNROUTED|NOT FOR PRODUCTION|routing (?:is )?(?:incomplete|not started)|schematic/netlist[^\r\n"]*incomplete') {
    $releaseBlockers += 'PCB title block still identifies the board as an incomplete placement/routing draft.'
}

$summary = @(
    "BomberCard release check: $ReleaseName",
    "KiCad: $kicadVersion",
    "Git commit: $gitCommit",
    "Git dirty: $gitDirty",
    "ERC: $($ercErrors.Count) error(s), $($ercWarnings.Count) warning(s)",
    "DRC/parity: $($drcErrors.Count) error(s), $($drcWarnings.Count) warning(s)",
    "Schematic parity mismatches: $($parityIssues.Count)",
    "Default DNP: $($defaultDnp -join ', ')"
)
$summary | Set-Content -LiteralPath (Join-Path $reportsDir 'release-check.txt') -Encoding UTF8

Write-Host "`n$($summary -join [Environment]::NewLine)"
if ($releaseBlockers.Count -gt 0) {
    Write-Host "`nRELEASE BLOCKED:" -ForegroundColor Red
    foreach ($blocker in $releaseBlockers) {
        Write-Host " - $blocker" -ForegroundColor Red
    }
    throw "Release checks failed. Reports remain in '$reportsDir'. No fabrication archive was created."
}

if ($ChecksOnly) {
    Write-Host "`nChecks passed. -ChecksOnly selected; no manufacturing files were exported." -ForegroundColor Green
    exit 0
}

$rawBom = Join-Path $assemblyDir '_bom-kicad-raw.csv'
Invoke-KiCad -Description 'Schematic BOM' -Arguments @(
    'sch', 'export', 'bom',
    '--output', $rawBom,
    '--fields', 'Reference,Value,Footprint,QUANTITY,DNP,Manufacturer,MPN,LCSC',
    '--labels', 'Designator,Comment,Footprint,Quantity,DNP,Manufacturer,MPN,LCSC',
    '--sort-field', 'Reference',
    $schematic
)

$rawBomRows = @(Import-Csv -LiteralPath $rawBom)
$effectiveDnp = @{}
foreach ($reference in $defaultDnp) {
    $effectiveDnp[(Get-ReferenceKey $reference)] = $true
}
foreach ($row in $rawBomRows) {
    if (Test-Truthy $row.DNP) {
        foreach ($reference in @(Get-Designators $row.Designator)) {
            $effectiveDnp[(Get-ReferenceKey $reference)] = $true
        }
    }
}
$effectiveDnpReferences = @($effectiveDnp.Keys | Sort-Object)
Add-Content -LiteralPath (Join-Path $reportsDir 'release-check.txt') `
    -Value "Effective DNP from schematic plus safety defaults: $($effectiveDnpReferences -join ', ')" `
    -Encoding UTF8

$rawPosition = Join-Path $assemblyDir '_positions-kicad-raw.csv'
Invoke-KiCad -Description 'KiCad component positions' -Arguments @(
    'pcb', 'export', 'pos',
    '--output', $rawPosition,
    '--side', 'both',
    '--format', 'csv',
    '--units', 'mm',
    '--smd-only',
    '--exclude-fp-th',
    '--exclude-dnp',
    $board
)

$positionRows = @(Import-Csv -LiteralPath $rawPosition | Where-Object {
        -not $effectiveDnp.ContainsKey((Get-ReferenceKey $_.Ref))
    })
$positionRefs = @{}
$jlcPositions = foreach ($row in $positionRows) {
    $positionRefs[(Get-ReferenceKey $row.Ref)] = $true
    [PSCustomObject][ordered]@{
        'Designator' = $row.Ref
        'Mid X'      = $row.PosX
        'Mid Y'      = $row.PosY
        'Layer'      = if ($row.Side -eq 'bottom') { 'Bottom' } else { 'Top' }
        'Rotation'   = $row.Rot
    }
}
Write-CsvUtf8 -Rows @($jlcPositions) -Path (Join-Path $assemblyDir 'cpl-jlcpcb.csv')

$masterBom = foreach ($row in $rawBomRows) {
    $designators = @(Get-Designators $row.Designator)
    foreach ($designator in $designators) {
        $referenceKey = Get-ReferenceKey $designator
        $isDnp = $effectiveDnp.ContainsKey($referenceKey)
        $note = ''
        if ($designator -eq 'C109') {
            $note = 'Default DNP; fit only after VSYS stability test.'
        } elseif ($designator -in @('C310', 'C311', 'C312')) {
            $note = 'Default DNP; NFC matching option. Select value only after antenna/VNA characterization.'
        } elseif ($designator -in @('C403', 'C404')) {
            $note = 'Default DNP; Sub-GHz pi-match option. Select only after assembled-board RF measurement.'
        } elseif ($designator -in @('C507', 'C508', 'C509')) {
            $note = 'Default DNP; LF resonance/RX trim option. Select only with the measured external coil.'
        } elseif (-not $row.Footprint) {
            $note = 'No purchasable PCB footprint; review as schematic/PCB structure.'
        }

        [PSCustomObject][ordered]@{
            'Designator' = $designator
            'Comment' = $row.Comment
            'Footprint' = $row.Footprint
            'Quantity' = if ($designators.Count -gt 1) { 1 } else { $row.Quantity }
            'Manufacturer' = $row.Manufacturer
            'MPN' = $row.MPN
            'LCSC' = $row.LCSC
            'DNP' = if ($isDnp) { 'DNP' } else { '' }
            'Assembly' = if ($isDnp) { 'Do not populate' } elseif ($positionRefs.ContainsKey($referenceKey)) { 'PCBA candidate' } else { 'Manual / review' }
            'Notes' = $note
        }
    }
}
Write-CsvUtf8 -Rows @($masterBom) -Path (Join-Path $assemblyDir 'bom-master.csv')

$jlcBom = foreach ($row in @($masterBom | Where-Object {
            -not $_.DNP -and $positionRefs.ContainsKey((Get-ReferenceKey $_.Designator))
        })) {
    [PSCustomObject][ordered]@{
        'Comment' = $row.Comment
        'Designator' = $row.Designator
        'Footprint' = $row.Footprint
        'Quantity' = $row.Quantity
        'Manufacturer' = $row.Manufacturer
        'Manufacturer Part Number' = $row.MPN
        'LCSC Part #' = $row.LCSC
    }
}
Write-CsvUtf8 -Rows @($jlcBom) -Path (Join-Path $assemblyDir 'bom-jlcpcb.csv')

$manualBom = @($masterBom | Where-Object {
        -not $_.DNP -and $_.Footprint -and -not $positionRefs.ContainsKey((Get-ReferenceKey $_.Designator))
    })
Write-CsvUtf8 -Rows $manualBom -Path (Join-Path $assemblyDir 'bom-manual-or-review.csv')

$procurementGaps = @($jlcBom | Where-Object {
        -not $_.Manufacturer -or -not $_.'Manufacturer Part Number' -or -not $_.'LCSC Part #'
    })
Write-CsvUtf8 -Rows $procurementGaps -Path (Join-Path $assemblyDir 'bom-procurement-gaps.csv')
if ($RequireCompleteProcurement -and $procurementGaps.Count -gt 0) {
    throw "PCBA release blocked: $($procurementGaps.Count) CPL part(s) lack Manufacturer, MPN, or LCSC data. See bom-procurement-gaps.csv."
}

Remove-Item -LiteralPath $rawBom, $rawPosition

Invoke-KiCad -Description 'Gerber layers' -Arguments @(
    'pcb', 'export', 'gerbers',
    '--output', $gerberDir,
    '--layers', 'F.Cu,In1.Cu,In2.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts',
    '--subtract-soldermask',
    '--check-zones',
    '--precision', '6',
    $board
)

Invoke-KiCad -Description 'Excellon drill files and drill map' -Arguments @(
    'pcb', 'export', 'drill',
    '--output', $gerberDir,
    '--format', 'excellon',
    '--drill-origin', 'absolute',
    '--excellon-units', 'mm',
    '--excellon-zeros-format', 'decimal',
    '--excellon-separate-th',
    '--generate-map',
    '--map-format', 'pdf',
    '--generate-report',
    '--report-path', (Join-Path $reportsDir 'drill-report.txt'),
    $board
)

Invoke-KiCad -Description 'IPC-D-356 electrical netlist' -Arguments @(
    'pcb', 'export', 'ipcd356',
    '--output', (Join-Path $releaseDir 'BomberCard.ipc'),
    $board
)

Invoke-KiCad -Description 'Board statistics' -Arguments @(
    'pcb', 'export', 'stats',
    '--output', (Join-Path $reportsDir 'board-stats.json'),
    '--format', 'json',
    '--units', 'mm',
    $board
)

Copy-Item -LiteralPath $schematic -Destination $sourceDir
Copy-Item -LiteralPath $board -Destination $sourceDir
foreach ($supportFile in @('assembly-variants.csv', 'preliminary-bom.csv')) {
    $path = Join-Path $hardwareDir $supportFile
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Copy-Item -LiteralPath $path -Destination $sourceDir
    }
}
Copy-Item -LiteralPath (Join-Path $manufacturingDir 'README.md') -Destination $sourceDir

$metadata = [PSCustomObject][ordered]@{
    project = 'BomberCard'
    release_name = $ReleaseName
    generated_utc = [DateTime]::UtcNow.ToString('o')
    kicad_version = $kicadVersion
    git_commit = $gitCommit
    git_dirty = $gitDirty
    default_dnp = $defaultDnp
    effective_dnp = $effectiveDnpReferences
    erc_errors = $ercErrors.Count
    erc_warnings = $ercWarnings.Count
    drc_errors = $drcErrors.Count
    drc_warnings = $drcWarnings.Count
    schematic_parity_mismatches = $parityIssues.Count
    procurement_gaps = $procurementGaps.Count
    prototype_only = $true
    production_approved = $false
    human_rotation_review_required = $true
    rf_and_nfc_tuning_required = $true
}
$metadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $releaseDir 'release-info.json') -Encoding UTF8

$manifestPath = Join-Path $releaseDir 'manifest-sha256.txt'
$manifestLines = foreach ($file in @(Get-ChildItem -LiteralPath $releaseDir -File -Recurse | Sort-Object FullName)) {
    if ($file.FullName -eq $manifestPath) { continue }
    $relative = $file.FullName.Substring($releaseDir.Length + 1).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $relative"
}
$manifestLines | Set-Content -LiteralPath $manifestPath -Encoding ASCII

$zipPath = Join-Path $manufacturingDir ("BomberCard-{0}.zip" -f $ReleaseName)
$zipHashPath = "$zipPath.sha256"
if ((Test-Path -LiteralPath $zipPath) -or (Test-Path -LiteralPath $zipHashPath)) {
    if (-not $Force) {
        throw "Release archive already exists: $zipPath. Pass -Force or choose another release name."
    }
    foreach ($oldPath in @($zipPath, $zipHashPath)) {
        if (Test-Path -LiteralPath $oldPath) { Remove-Item -LiteralPath $oldPath -Force }
    }
}

Compress-Archive -Path (Join-Path $releaseDir '*') -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
"$zipHash  $([IO.Path]::GetFileName($zipPath))" | Set-Content -LiteralPath $zipHashPath -Encoding ASCII

Write-Host "`nManufacturing export completed:" -ForegroundColor Green
Write-Host "  Directory: $releaseDir"
Write-Host "  Archive:   $zipPath"
Write-Host "  SHA-256:   $zipHash"
Write-Host 'The archive is a prototype handoff, not an RF/NFC or production approval.' -ForegroundColor Yellow
