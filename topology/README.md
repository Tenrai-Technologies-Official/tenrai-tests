# Peer topology

Starts development nodes on separate loopback addresses and checks peer
discovery, graph connectivity and refilled outbound slots after nodes stop.

```sh
py -3.14 topology.py 16 180
```

The arguments select the node count and settle time in seconds.
`TENRAI_REPO` selects the node checkout. `TOPOLOGY_ROOT` sets the scratch
directory, which defaults to `run/`. Reports go to `results/`.
