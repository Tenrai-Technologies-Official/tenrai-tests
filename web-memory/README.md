# Browser memory

Sample browser process memory while a page stays open. Chrome also reports
JavaScript heap and DOM counters.

```sh
node memwatch.mjs http://127.0.0.1:28778/ --minutes 10 --every 30
node memwatch-firefox.mjs http://127.0.0.1:28778/ --minutes 10 --every 30
```

Set `CHROME` or `FIREFOX`, or pass `--chrome` or `--firefox`, to select an
executable. Use a distinct `--port` for each concurrent run.
