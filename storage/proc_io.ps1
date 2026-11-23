param([string[]]$Names = @('tenraid'), [int]$Seconds = 60, [string]$Drive = $env:SystemDrive)
# File I/O and CPU per process over one interval, and the drive's own counters.
function Snap {
    $map = @{}
    foreach ($p in Get-CimInstance Win32_Process) {
        if ($Names -contains [IO.Path]::GetFileNameWithoutExtension($p.Name)) { $map[$p.ProcessId] = $p }
    }
    $map
}
function Disk { Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk | Where-Object { $_.Name -like "*$Drive*" } }

$disk0 = Disk
$a = Snap
$t0 = Get-Date
Start-Sleep -Seconds $Seconds
$b = Snap
$t1 = Get-Date
$disk1 = Disk
$span = ($t1 - $t0).TotalSeconds
foreach ($id in $b.Keys) {
    if (-not $a.ContainsKey($id)) { continue }
    $p = $b[$id]; $q = $a[$id]
    '{0,-26} pid {1,6}  read {2,7:N2} MiB/s {3,6:N0}/s  write {4,7:N2} MiB/s {5,6:N0}/s  cpu {6,5:N2} cores  ws {7,6:N0} MB' -f $p.Name, $id,
        (($p.ReadTransferCount - $q.ReadTransferCount) / $span / 1MB), (($p.ReadOperationCount - $q.ReadOperationCount) / $span),
        (($p.WriteTransferCount - $q.WriteTransferCount) / $span / 1MB), (($p.WriteOperationCount - $q.WriteOperationCount) / $span),
        ((($p.KernelModeTime + $p.UserModeTime) - ($q.KernelModeTime + $q.UserModeTime)) / 1e7 / $span), ($p.WorkingSetSize / 1MB)
}
if ($disk0 -and $disk1) {
    $f = $disk1.Frequency_PerfTime
    $reads = $disk1.DiskReadsPerSec - $disk0.DiskReadsPerSec
    $writes = $disk1.DiskWritesPerSec - $disk0.DiskWritesPerSec
    $readMs = if ($reads -gt 0) { ($disk1.AvgDiskSecPerRead - $disk0.AvgDiskSecPerRead) / $f / $reads * 1000 } else { 0 }
    $writeMs = if ($writes -gt 0) { ($disk1.AvgDiskSecPerWrite - $disk0.AvgDiskSecPerWrite) / $f / $writes * 1000 } else { 0 }
    $idle = ($disk1.PercentIdleTime - $disk0.PercentIdleTime) / ($disk1.Timestamp_Sys100NS - $disk0.Timestamp_Sys100NS) * 100
    'disk {0}: read {1:N2} MiB/s ({2:N0}/s, {3:N1} ms), write {4:N2} MiB/s ({5:N0}/s, {6:N1} ms), idle {7:N0} %' -f $disk1.Name,
        (($disk1.DiskReadBytesPerSec - $disk0.DiskReadBytesPerSec) / $span / 1MB), ($reads / $span), $readMs,
        (($disk1.DiskWriteBytesPerSec - $disk0.DiskWriteBytesPerSec) / $span / 1MB), ($writes / $span), $writeMs, $idle
}
