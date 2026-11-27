"""Check the Windows PowerShell packager flags seen by its native child process."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from node_checkout import node_checkout

REPO = node_checkout(Path(__file__).resolve().parents[2])
DRIVER = REPO / "scripts/package-wasm.ps1"
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
VARIABLES = ("RUSTFLAGS", "CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS",
             "CARGO_BUILD_JOBS", "CARGO_TARGET_DIR")
CAPTURE = '''import json, os, sys
from pathlib import Path
names = ("RUSTFLAGS", "CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS",
         "CARGO_BUILD_JOBS", "CARGO_TARGET_DIR")
value = {name: os.environ.get(name) for name in names}
value["encoded_present"] = "CARGO_ENCODED_RUSTFLAGS" in os.environ
Path(sys.argv[1]).write_text(json.dumps(value), encoding="utf-8")
'''
FAKE_PACK = r'''
[IO.File]::WriteAllText((Join-Path $env:TENRAI_FLAGS_SCRATCH 'pack-argv.json'),
    (ConvertTo-Json -InputObject @($args) -Compress), [Text.UTF8Encoding]::new($false))
& $env:TENRAI_FLAGS_PYTHON '-I' '-B' $env:TENRAI_FLAGS_CAPTURE (Join-Path $env:TENRAI_FLAGS_SCRATCH 'pack-env.json')
if ($LASTEXITCODE -ne 0) { throw 'Native capture failed' }
if ($env:TENRAI_FLAGS_FAILURE -eq 'pack') { exit 7 }
exit 0
'''
HARNESS = r'''
param([string]$Driver, [string]$Scratch, [string]$Variant, [string]$Failure, [string]$GuardMode,
      [string]$SavedEncoded, [string]$Offline)
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($Driver, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'The real packager must parse before isolation' }
$functions = @($tree.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Build-Package'
}, $true))
if ($functions.Count -ne 1) { throw 'Expected exactly one real Build-Package definition' }
$guards = @($tree.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text.Contains('CARGO_ENCODED_RUSTFLAGS')
})
if ($guards.Count -ne 1) { throw 'Expected exactly one actual ambient encoded-flags guard' }
$calls = @($tree.FindAll({ param($node)
    $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Build-Package'
}, $true))
if ($calls.Count -ne 2) { throw 'Expected actual plain and threaded Build-Package calls' }
$remaps = @($tree.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.AssignmentStatementAst] -and
    $_.Left -is [Management.Automation.Language.VariableExpressionAst] -and
    $_.Left.VariablePath.UserPath -in @('remapPairs', 'remap')
})
if ($remaps.Count -ne 2) { throw 'Expected actual two-step release remap construction' }

$repositoryRoot = Join-Path $Scratch 'repo'
$crateRoot = Join-Path $repositoryRoot 'crate'
$TargetRoot = Join-Path $Scratch 'targets'
$null = New-Item -ItemType Directory -Path $crateRoot -Force
$null = New-Item -ItemType Directory -Path $TargetRoot -Force
$wasmPackPath = Join-Path $Scratch 'fake-pack.ps1'
$wasmOptArguments = @('--test-fixture-only')
$settings = @{ name = 'tenrai_tenq_wasm' }
$Jobs = 3
$OfflineTools = $Offline -eq 'true'
$global:preflights = 0
$global:optimizations = 0
function Invoke-QualificationPreflight { $global:preflights += 1 }
function Optimize-Wasm([string]$Path, [string[]]$Arguments) {
    $global:optimizations += 1
    if ($env:TENRAI_FLAGS_FAILURE -eq 'optimize') { throw 'Fixture optimization failed' }
}
Invoke-Expression $functions[0].Extent.Text
$fixtureRoot = [IO.Path]::GetPathRoot($Scratch)
$cargoHome = (Join-Path $fixtureRoot 'fixture-cargo').Replace('\', '/')
$sysroot = Join-Path $fixtureRoot 'fixture-toolchain'
$stdSources = Join-Path $sysroot 'lib/rustlib/src/rust'
$stdSources = $stdSources.Replace('/', '\')
foreach ($assignment in $remaps) { Invoke-Expression $assignment.Extent.Text }

function Capture-Native([string]$Name) {
    & $env:TENRAI_FLAGS_PYTHON '-I' '-B' $env:TENRAI_FLAGS_CAPTURE (Join-Path $Scratch $Name)
    if ($LASTEXITCODE -ne 0) { throw 'Native capture failed' }
}
Capture-Native 'entry-env.json'
$guardThrew = $false
$guardError = $null
if ($GuardMode -ne 'skip') {
    try { Invoke-Expression $guards[0].Extent.Text }
    catch { $guardThrew = $true; $guardError = $_.Exception.Message }
}
if ($SavedEncoded -ne 'inherit') { $env:CARGO_ENCODED_RUSTFLAGS = $env:TENRAI_FLAGS_SAVED_ENCODED }
Capture-Native 'before-env.json'
$beforeLocation = (Get-Location).Path
$beforeEncodedPresent = Test-Path Env:CARGO_ENCODED_RUSTFLAGS
$beforeEncodedValue = $env:CARGO_ENCODED_RUSTFLAGS
$buildThrew = $false
$buildError = $null
if (!$guardThrew -and $GuardMode -ne 'only') {
    try {
        $index = if ($Variant -eq 'threads') { 1 } else { 0 }
        Invoke-Expression $calls[$index].Extent.Text
    }
    catch { $buildThrew = $true; $buildError = $_.Exception.Message }
}
Capture-Native 'after-env.json'
$result = [ordered]@{
    guard_threw = $guardThrew; guard_error = $guardError
    build_threw = $buildThrew; build_error = $buildError
    before_location = $beforeLocation; after_location = (Get-Location).Path
    before_encoded_present = $beforeEncodedPresent; before_encoded_value = $beforeEncodedValue
    after_encoded_present = (Test-Path Env:CARGO_ENCODED_RUSTFLAGS)
    after_encoded_value = $env:CARGO_ENCODED_RUSTFLAGS
    preflights = $global:preflights; optimizations = $global:optimizations
}
[IO.File]::WriteAllText((Join-Path $Scratch 'result.json'),
    ($result | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
'''


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required; no other OS/runtime is qualified by this test")
class PackageWasmNativeFlags(unittest.TestCase):
    def run_case(self, *, variant="plain", failure="none", guard="normal", inherited_encoded="",
                 saved_encoded="inherit", offline=True):
        self.assertTrue(DRIVER.is_file(), f"actual main repository packager missing: {DRIVER}")
        with tempfile.TemporaryDirectory(prefix="tenrai-wasm-flags-") as directory:
            scratch = Path(directory)
            for name, text in (("capture.py", CAPTURE), ("fake-pack.ps1", FAKE_PACK), ("harness.ps1", HARNESS)):
                (scratch / name).write_text(text, encoding="utf-8", newline="\n")
            environment = {**os.environ, "RUSTFLAGS": "--cfg caller_ambient",
                "CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_RUSTFLAGS": "--cfg caller_target",
                "CARGO_ENCODED_RUSTFLAGS": inherited_encoded, "CARGO_BUILD_JOBS": "11",
                "CARGO_TARGET_DIR": "caller-target", "TENRAI_FLAGS_PYTHON": sys.executable,
                "TENRAI_FLAGS_CAPTURE": str(scratch / "capture.py"), "TENRAI_FLAGS_SCRATCH": str(scratch),
                "TENRAI_FLAGS_FAILURE": failure, "TENRAI_FLAGS_SAVED_ENCODED": saved_encoded}
            completed = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(scratch / "harness.ps1"), "-Driver", str(DRIVER), "-Scratch", str(scratch),
                "-Variant", variant, "-Failure", failure, "-GuardMode", guard,
                "-SavedEncoded", "inherit" if saved_encoded == "inherit" else "replace",
                "-Offline", str(offline).lower()], env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            output = {"fixture-root": scratch.anchor}
            for name in ("result", "entry-env", "before-env", "after-env", "pack-env", "pack-argv"):
                path = scratch / (name + ".json")
                if path.exists():
                    output[name] = json.loads(path.read_text(encoding="utf-8-sig"))
            return output

    def assert_restored(self, output):
        before, after = output["before-env"], output["after-env"]
        for name in VARIABLES:
            if name != "CARGO_ENCODED_RUSTFLAGS":
                self.assertEqual(after[name], before[name], f"caller variable not restored: {name}")
        state = output["result"]
        self.assertEqual(state["after_encoded_present"], state["before_encoded_present"])
        self.assertEqual(state["after_encoded_value"], state["before_encoded_value"])
        # The top-level guard removes an empty value. A Windows PowerShell native
        # child can still see that stale entry until the build sets its real flags.
        # Restore the caller's post-guard state, including absence, at both boundaries.
        self.assertEqual(after["encoded_present"], state["before_encoded_present"])
        self.assertEqual(after["CARGO_ENCODED_RUSTFLAGS"], state["before_encoded_value"])
        self.assertEqual(state["after_location"], state["before_location"])

    def assert_native_tokens(self, output, *, threads):
        native = output["pack-env"]
        self.assertTrue(native["encoded_present"], "native Cargo boundary must have explicit nonempty encoded flags")
        self.assertTrue(native["CARGO_ENCODED_RUSTFLAGS"])
        actual = native["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
        expected = ["--cfg", "caller_ambient", "--cfg", "caller_target"]
        if threads:
            expected += ["-C", "strip=none", "-C", "target-feature=+atomics,+bulk-memory,+mutable-globals",
                "-C", "link-arg=--shared-memory", "-C", "link-arg=--import-memory",
                "-C", "link-arg=--max-memory=4294967296", "-C", "link-arg=--export=__wasm_init_tls",
                "-C", "link-arg=--export=__tls_size", "-C", "link-arg=--export=__tls_align",
                "-C", "link-arg=--export=__tls_base", "-C", "link-arg=--export=__heap_base",
                "-C", "link-arg=--export=__stack_pointer"]
        root = Path(output["fixture-root"])
        cargo = root / "fixture-cargo"
        sysroot = root / "fixture-toolchain"
        sources = sysroot / "lib" / "rustlib" / "src" / "rust"
        expected += [
            f"--remap-path-prefix={cargo.as_posix()}=/cargo",
            f"--remap-path-prefix={cargo}=/cargo",
            f"--remap-path-prefix={sysroot}=/rust-sysroot",
            f"--remap-path-prefix={sysroot.as_posix()}=/rust-sysroot",
            f"--remap-path-prefix={sources}=/rust",
            f"--remap-path-prefix={sources.as_posix()}=/rust",
        ]
        self.assertEqual(actual, expected, "native flags must preserve separate -C/value and remap tokens")
        self.assertEqual(native["RUSTFLAGS"].split(), actual)
        self.assertEqual(native["CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_RUSTFLAGS"].split(), actual)
        self.assertEqual(native["CARGO_BUILD_JOBS"], "3")
        self.assertEqual(Path(native["CARGO_TARGET_DIR"]).name,
                         "tenrai_tenq_wasm_threads" if threads else "tenrai_tenq_wasm")
        argv = output["pack-argv"]
        self.assertIn("--locked", argv)
        self.assertIn("--offline", argv)
        self.assertEqual("build-std=panic_abort,std" in argv, threads)
        self.assertIn("--mode", argv)
        self.assertIn("no-install", argv)

    def test_inherited_empty_encoded_flags_cannot_suppress_real_plain_or_thread_flags(self):
        captures = []
        for variant in ("plain", "threads"):
            with self.subTest(variant=variant):
                output = self.run_case(variant=variant)
                self.assertTrue(output["entry-env"]["encoded_present"])
                self.assertEqual(output["entry-env"]["CARGO_ENCODED_RUSTFLAGS"], "")
                self.assertFalse(output["result"]["guard_threw"])
                self.assertFalse(output["result"]["build_threw"])
                self.assertEqual(output["result"]["optimizations"], 1)
                self.assert_native_tokens(output, threads=variant == "threads")
                self.assert_restored(output)
                captures.append(output["pack-env"]["CARGO_ENCODED_RUSTFLAGS"])
        self.assertEqual(len(captures), 2)
        self.assertNotEqual(captures[0], captures[1])

    def test_function_restores_nonempty_saved_encoded_flags_after_success(self):
        # Function-scoped restoration is distinct from the top-level ambient guard.
        saved = "--cfg\x1fcaller_saved_encoded"
        output = self.run_case(variant="threads", guard="skip", saved_encoded=saved)
        self.assertFalse(output["result"]["build_threw"])
        self.assert_native_tokens(output, threads=True)
        self.assertEqual(output["after-env"]["CARGO_ENCODED_RUSTFLAGS"], saved)
        self.assert_restored(output)

    def test_failure_restores_caller_flags_for_packager_and_optimizer(self):
        for failure in ("pack", "optimize"):
            with self.subTest(failure=failure):
                output = self.run_case(variant="threads", failure=failure)
                self.assertTrue(output["result"]["build_threw"])
                expected = "wasm-pack did not build" if failure == "pack" else "Fixture optimization failed"
                self.assertIn(expected, output["result"]["build_error"])
                self.assertEqual(output["result"]["optimizations"], 0 if failure == "pack" else 1)
                self.assert_native_tokens(output, threads=True)
                self.assert_restored(output)

    def test_actual_top_level_guard_rejects_nonempty_encoded_flags_in_both_modes(self):
        for offline in (False, True):
            with self.subTest(offline=offline):
                output = self.run_case(guard="only", inherited_encoded="--cfg\x1fambient_encoded", offline=offline)
                self.assertTrue(output["result"]["guard_threw"])
                self.assertIn("Unset CARGO_ENCODED_RUSTFLAGS", output["result"]["guard_error"])
                self.assertNotIn("pack-env", output)
                self.assertEqual(output["result"]["preflights"], 0)
                self.assertEqual(output["result"]["optimizations"], 0)
                self.assertEqual(output["entry-env"], output["after-env"])


if __name__ == "__main__":
    unittest.main()
