# Build and vendor browser packages, preserving their per-package source receipts.
param(
    [Parameter(Mandatory = $true)][string] $Out,
    [string] $Work,
    [ValidateRange(1, 64)][int] $Jobs = 1,
    [string] $Holder,
    [string] $Scratch
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$tests = (Resolve-Path -LiteralPath (Join-Path $here '..')).Path
$github = (Resolve-Path -LiteralPath (Join-Path $tests '..')).Path
$repo = if ($env:TENRAI_REPO) { $env:TENRAI_REPO } else { Join-Path $github 'tenrai-core' }
$runtime = Join-Path $here 'release_runtime.py'
$guard = Join-Path $tests 'qualification\envcheck.py'
$started = Get-Date
$Out = [System.IO.Path]::GetFullPath($Out)
if (!$Work) { $Work = $Out }
$Work = [System.IO.Path]::GetFullPath($Work)
function Python([string[]] $Arguments, [string] $Failure) {
    $python = (Get-Command py -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $output = & $python -3.14 -E -s -B @Arguments
    if ($LASTEXITCODE -ne 0) { $output; throw $Failure }
    $output
}
Python @('-c', 'import sys; sys.path.insert(0, sys.argv[1]); from release_evidence import artifact_root; artifact_root(sys.argv[2])', $here, $Work) 'the output directory is not an external artifact root'
if ($Out -ine $Work -and !$Out.StartsWith($Work.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Out must be Work or its child.' }
if (!$Scratch) { $Scratch = Join-Path $Work 'scratch' }
$Scratch = [System.IO.Path]::GetFullPath($Scratch)
if (!$Scratch.StartsWith($Work.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Scratch must be a child of Work.'
}
$attemptName = 'wasm-' + [guid]::NewGuid().ToString('N')
$attempt = Join-Path $Out $attemptName
$scratchAttempt = Join-Path $Scratch $attemptName
New-Item -ItemType Directory -Path $attempt, $scratchAttempt -Force | Out-Null
$env:TMP = Join-Path $scratchAttempt 'temp'
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$env:CARGO_BUILD_JOBS = "$Jobs"
Set-Location -LiteralPath $repo
function Step([string] $What) { '{0:N0} s {1}' -f ((Get-Date) - $started).TotalSeconds, $What }
function Begin-Step([string] $Name, [string] $Kind, [string[]] $Repos, [string[]] $Changes) {
    $begin = @($runtime, 'begin', '--work', $Work, '--name', "wasm-$Name", '--kind', $Kind, '--envcheck', $guard, '--commit-gb', '8', '--room', "$Scratch=8")
    foreach ($source in $Repos) { $begin += @('--repo', $source) }
    foreach ($change in $Changes) { $begin += @('--allow-change', $change) }
    if ($Holder) { $begin += @('--holder', $Holder) }
    (Python $begin "the $Name source boundary or preflight was refused" | Select-Object -Last 1).Trim()
}
function End-Step([string] $Ticket, [int] $Code, [string[]] $Artifacts) {
    $end = @($runtime, 'end', '--work', $Work, '--ticket', $Ticket, '--exit-code', "$Code")
    foreach ($artifact in $Artifacts) { $end += @('--artifact-tree', $artifact) }
    Python $end 'the command or source boundary failed'
}
function Run-Step([string] $Name, [string] $Kind, [string[]] $Repos, [string[]] $Changes, [string] $Program, [string[]] $Arguments, [string[]] $Artifacts, [string[]] $Finalize) {
    $ticket = Begin-Step $Name $Kind $Repos $Changes
    $log = Join-Path $attempt "$Name.log"
    $code = 1
    try {
        $command = (Get-Command $Program -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            # Windows PowerShell marks a redirected native command failed as soon as it writes to stderr, as wasm-pack
            # does; only its exit code says whether it failed, and a command that cannot start throws.
            & $command @Arguments *> $log
            $code = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $previousPreference }
        if ($null -eq $code) { $code = 1 }
        if ($code -eq 0 -and $Finalize) {
            $code = 1
            Python $Finalize 'the built WASM package does not match its input sources' | Add-Content -LiteralPath $log
            $code = 0
        }
    }
    finally { End-Step $ticket $code $Artifacts }
    if ($code -ne 0) { Get-Content -LiteralPath $log -Tail 20; throw "$Name failed with exit $code" }
}
foreach ($package in 'wallet', 'tenq') {
    $folder = Join-Path $attempt $package
    New-Item -ItemType Directory -Path $folder | Out-Null
    $ticket = Begin-Step "$package-record" 'test' @($repo) @()
    $code = 1
    try {
        $revision = (Python @((Join-Path $here 'wasm_source_tree.py'), 'record', $folder, '--package', $package) "the $package sources were not recorded" | Select-Object -Last 1).Trim()
        $code = 0
    }
    finally { End-Step $ticket $code @() }
    Step "$package revision $revision"
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $repo 'scripts\package-wasm.ps1'),
        '-Revision', $revision, '-Package', $package, '-Jobs', "$Jobs", '-PreflightScript', $guard,
        '-TargetRoot', (Join-Path $scratchAttempt "$package-target"))
    if ($Holder) { $arguments += @('-Holder', $Holder) }
    $packageCrate = if ($package -eq 'wallet') { 'wasm' } else { 'tenq-wasm' }
    $packageOutput = Join-Path (Join-Path (Join-Path $repo 'crates') $packageCrate) 'pkg'
    Run-Step "$package-build" 'build' @($repo) @() 'powershell' $arguments @($packageOutput, $folder) @((Join-Path $here 'wasm_source_tree.py'), 'finalize', $folder, '--package', $package)
    Step "$package WASM built"
}
$sdk = Join-Path $github 'tenrai-sdk'
$wallet = Join-Path $github 'tenrai-webwallet'
Run-Step 'vendor-wallet-wasm' 'mutation' @($repo, $sdk, $wallet) @("$sdk=wasm", "$sdk=src/wallet/runtime.ts", "$wallet=vendor/tenrai-wasm") 'py' @('-3.14', '-E', '-s', '-B', (Join-Path $here 'vendor_wasm.py'))
$miner = Join-Path $github 'tenrai-browser-miner'
Push-Location -LiteralPath $miner
try { Run-Step 'vendor-tenq' 'mutation' @($repo, $miner) @("$miner=public/vendor/tenq") 'node' @('scripts/vendor-tenq.mjs', (Join-Path $repo 'crates\tenq-wasm\pkg')) }
finally { Pop-Location }
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $repo 'scripts\package-prover.ps1'),
    '-Jobs', "$Jobs", '-PreflightScript', $guard, '-TargetRoot', (Join-Path $scratchAttempt 'prover-target'))
if ($Holder) { $arguments += @('-Holder', $Holder) }
Run-Step 'prover-build' 'build' @($repo) @() 'powershell' $arguments @((Join-Path $repo 'crates\tsp2-prover-wasm\package')) @()
Step 'browser prover built'
Run-Step 'vendor-prover' 'mutation' @($repo, $sdk, $wallet) @("$sdk=prover", "$sdk=src/wallet/prover-pins.ts", "$wallet=vendor/tenrai-prover") 'py' @('-3.14', '-E', '-s', '-B', (Join-Path $here 'vendor_prover.py'))
Step 'done'
