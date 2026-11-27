# Run browser rounds serially against frozen binaries with one source/resource boundary per round.
param(
    [Parameter(Mandatory = $true)][string] $Bin,
    [Parameter(Mandatory = $true)][string] $OutDir,
    [string[]] $Runs = @('normal', 'reference', 'sdk', 'gateway'),
    [string] $Work,
    [ValidateRange(1, 64)][int] $Jobs = 1,
    [string] $Holder,
    [string] $Scratch,
    [string] $Data
)
$ErrorActionPreference = 'Stop'
$Runs = @($Runs | ForEach-Object { $_ -split ',' })
if (@($Runs | Select-Object -Unique).Count -ne $Runs.Count) { throw 'Browser rounds must be distinct.' }
$knownRuns = @('normal', 'normal-long', 'plain', 'gateway', 'firefox', 'reference', 'sdk')
foreach ($run in $Runs) { if ($run -notin $knownRuns) { throw "unknown run $run" } }
$tests = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$github = (Resolve-Path -LiteralPath (Join-Path $tests '..')).Path
$repo = $env:TENRAI_REPO
if (!$repo) {
    $repo = py -3.14 -E -s -B (Join-Path $PSScriptRoot 'node_checkout.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the node checkout.' }
}
$runtime = Join-Path $PSScriptRoot 'release_runtime.py'
$guard = Join-Path $tests 'qualification\envcheck.py'
$python = (Get-Command py -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$node = (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
if (!$Work) { $Work = $OutDir }
$Work = [System.IO.Path]::GetFullPath($Work)
& $python -3.14 -E -s -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from release_evidence import artifact_root; artifact_root(sys.argv[2])' $PSScriptRoot $Work
if ($LASTEXITCODE -ne 0) { throw 'Work must be an external artifact directory.' }
if ($OutDir -ine $Work -and !$OutDir.StartsWith($Work.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutDir must be Work or its child.'
}
if ($Data) {
    if ($Scratch) { throw 'Use either Scratch or Data, not both.' }
    $Scratch = $Data
}
if (!$Scratch) { $Scratch = Join-Path $Work 'scratch' }
$Scratch = [System.IO.Path]::GetFullPath($Scratch)
if (!$Scratch.StartsWith($Work.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Scratch must be a child of Work.'
}
$attempt = Join-Path $Scratch ('browser-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $attempt, $OutDir -Force | Out-Null
$env:TEMP = Join-Path $attempt 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP | Out-Null
$env:CARGO_BUILD_JOBS = "$Jobs"
$tenraid = Join-Path $Bin 'tenraid.exe'
$prover = Join-Path $Bin 'tenrai-tsp2-prove.exe'
$env:TENRAI_TSP2_PROVER = $prover
$failed = 0
Set-Location -LiteralPath (Join-Path $tests 'browser-wallet')
foreach ($run in $Runs) {
    $round = Join-Path $attempt $run
    New-Item -ItemType Directory -Path $round | Out-Null
    $nodeData = Join-Path $round 'node-data'
    $output = Join-Path $OutDir "$run.json"
    $log = Join-Path $OutDir "$run.log"
    $shotDir = Join-Path $OutDir "$run-shots"
    foreach ($previous in @($output, $log, $shotDir, (Join-Path $OutDir "$run-gateway.log"), (Join-Path $OutDir "$run-server.log"))) {
        & $python -3.14 -E -s -B $runtime archive --work $Work --path $previous
        if ($LASTEXITCODE -ne 0) { throw 'An earlier browser artifact could not be preserved.' }
    }
    $sources = @($repo, $tests)
    $common = @('--tenraid', $tenraid, '--repo', $repo, '--data', $nodeData, '--out', $output)
    $shots = @('--shots', $shotDir)
    $gateway = $null
    switch ($run) {
        'normal' { $sources += (Join-Path $github 'tenrai-webwallet'); $arguments = @('e2e.mjs', '--wallet', $sources[-1]) + $common + $shots }
        'normal-long' { $sources += (Join-Path $github 'tenrai-webwallet'); $arguments = @('e2e.mjs', '--long', '--wallet', $sources[-1]) + $common + $shots }
        'plain' { $sources += (Join-Path $github 'tenrai-webwallet'); $arguments = @('e2e.mjs', '--plain', '--wallet', $sources[-1]) + $common + $shots }
        'gateway' {
            $sources += (Join-Path $github 'tenrai-webwallet')
            # Settings are written beside the executable, outside both frozen binaries and node-data.
            $gatewayDir = Join-Path $round 'gateway'
            New-Item -ItemType Directory -Path $gatewayDir | Out-Null
            $originalGateway = Join-Path $Bin 'tenrai-api.exe'
            $gateway = Join-Path $gatewayDir 'tenrai-api.exe'
            $gatewayHash = (Get-FileHash -LiteralPath $originalGateway -Algorithm SHA256).Hash.ToLowerInvariant()
            Copy-Item -LiteralPath $originalGateway -Destination $gateway
            if ((Get-FileHash -LiteralPath $gateway -Algorithm SHA256).Hash.ToLowerInvariant() -ne $gatewayHash) { throw 'The gateway copy differs from its frozen input.' }
            $arguments = @('e2e.mjs', '--gateway', $gateway, '--wallet', $sources[-1]) + $common + $shots
            $sources += (Join-Path $github 'tenrai-api')
        }
        'firefox' {
            $sources += (Join-Path $github 'tenrai-webwallet')
            $firefox = if ($env:FIREFOX) { $env:FIREFOX } else {
                $installed = Join-Path $env:ProgramFiles 'Mozilla Firefox/firefox.exe'
                if (Test-Path -LiteralPath $installed -PathType Leaf) { $installed } else { 'firefox.exe' }
            }
            $arguments = @('e2e.mjs', '--firefox', $firefox, '--wallet', $sources[-1]) + $common + $shots
        }
        'reference' { $sources += (Join-Path $github 'tenrai-reference-webwallet'); $sources += (Join-Path $github 'tenrai-sdk'); $arguments = @('e2e-reference.mjs', '--wallet', $sources[-2]) + $common + $shots }
        'sdk' { $sources += (Join-Path $github 'tenrai-sdk'); $arguments = @('e2e-sdk.mjs', '--sdk', $sources[-1], '--prover', $prover) + $common }
    }
    $begin = @($runtime, 'begin', '--work', $Work, '--name', "browser-$run", '--kind', 'test', '--envcheck', $guard, '--frozen', $Bin, '--commit-gb', '8', '--room', "$Scratch=8")
    foreach ($source in $sources) { $begin += @('--repo', $source) }
    if ($Holder) { $begin += @('--holder', $Holder) }
    if ($gateway) { $begin += @('--file', "$gateway=$gatewayHash") }
    $check = & $python -3.14 -E -s -B @begin 2>&1
    if ($LASTEXITCODE -ne 0) { "NOT STARTED $run $check"; $failed++; continue }
    $ticket = ($check | Select-Object -Last 1).ToString().Trim()
    $code = 1
    $boundary = 1
    try {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            # A redirected native command counts as failed in Windows PowerShell once it writes to stderr; only its exit
            # code says whether it failed, and a command that cannot start throws.
            & $node @arguments *> $log
            $code = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $previousPreference }
        if ($null -eq $code) { $code = 1 }
    }
    catch { $_ | Out-String | Add-Content -LiteralPath $log; $code = 1 }
    finally {
        $end = @($runtime, 'end', '--work', $Work, '--ticket', $ticket, '--exit-code', "$code", '--artifact', $output, '--artifact', $log)
        foreach ($kept in 'gateway.log', 'server.log') {
            $source = Join-Path $nodeData $kept
            if (Test-Path -LiteralPath $source) {
                $destination = Join-Path $OutDir "$run-$kept"
                Copy-Item -LiteralPath $source -Destination $destination
                $end += @('--artifact', $destination)
            }
        }
        if (Test-Path -LiteralPath $shotDir) { $end += @('--artifact-tree', $shotDir) }
        & $python -3.14 -E -s -B @end
        $boundary = $LASTEXITCODE
    }
    if ($code -ne 0 -or $boundary -ne 0) { $failed++ }
    Get-Content -LiteralPath $log | Where-Object { $_ -match 'FAIL|passed|NOT RUN' } |
        ForEach-Object { $_.ToString().Substring(0, [Math]::Min(260, $_.ToString().Length)) }
    "{0} {1} native {2} boundary {3}" -f (Get-Date -Format 'HH:mm:ss'), $run, $code, $boundary
}
exit $failed
