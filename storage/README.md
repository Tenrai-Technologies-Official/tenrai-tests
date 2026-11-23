# Storage diagnostics

| Tool | Purpose |
| --- | --- |
| `rocks_events.py` | RocksDB flush, compaction and table statistics by column family |
| `proc_io.ps1` | Sample process I/O, CPU and disk counters on Windows |
| `compare_runs.py` | Compare two load reports using their paths without the suffix |

```sh
py -3.14 -I rocks_events.py "PATH_TO_NODE_DATA/traidag.rocks"
```

`--since` accepts `YYYY/MM/DD-HH:MM:SS`. `--hourly` accepts a comma-separated
list of column families. Pass `-Drive` to `proc_io.ps1` to select a volume.

For deterministic load, use the node repository's `storage_load` test.
`TENRAI_LOAD_DIR` sets its scratch directory and `TENRAI_LOAD_PAYMENTS` sets
the number of payments per cut.
