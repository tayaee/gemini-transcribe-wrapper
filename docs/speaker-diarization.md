# Diarizing Tip (Speaker Labels)

`--diarized-srt-file` produces `.diarized.srt` with raw speaker ids (`spk:0`, `spk:1`, ...). Map them to real names one pass at a time with `--speakers`: delete `.diarized.srt`, re-run with a more complete map, repeat until no `spk:` string remains.

```bash
# Pass 0: no --speakers — every cue keeps its raw spk:# tag.
gtw --diarized-srt-file=interview.diarized.srt interview.mp4

# Wrapper logs the unmapped speakers and prints the re-render recipe:
# WARNING Some speakers are not covered by --speakers mapping.
#   Speaker map: spk:0 spk:1 spk:2
#   Unmapped: spk:0, spk:1, spk:2
# WARNING To re-render with names, delete the .diarized.srt and re-run with the
#   option, editing the Name# entries: rm 'interview.diarized.srt' &&
#   gtw 'interview.mp4' --diarized-srt-file='interview.diarized.srt' --speakers 'spk:0=Name0;spk:1=Name1;spk:2=Name2;'

# Pass 1: rename spk:0 → Host. Edit the recipe, delete the old .diarized.srt, re-run.
rm interview.diarized.srt
gtw --diarized-srt-file=interview.diarized.srt interview.mp4 --speakers 'spk:0=Host;'

# Pass 2: add spk:1 → Guest. Same drill.
rm interview.diarized.srt
gtw --diarized-srt-file=interview.diarized.srt interview.mp4 --speakers 'spk:0=Host;spk:1=Guest;'

# Pass 3: add spk:2 → Interpreter. Now every tag is a real name.
rm interview.diarized.srt
gtw --diarized-srt-file=interview.diarized.srt interview.mp4 --speakers 'spk:0=Host;spk:1=Guest;spk:2=Interpreter;'
```

Simulation of the iterative renaming (`.diarized.srt` excerpt after each pass):

```
# Pass 0 (no --speakers): all raw ids
[spk:0] Hello everyone, welcome to the show.
[spk:1] Today we have a special guest with us.
[spk:2] Thanks for having me, it's great to be here.

# Pass 1 (--speakers 'spk:0=Host;')
[Host]     Hello everyone, welcome to the show.
[spk:1]    Today we have a special guest with us.
[spk:2]    Thanks for having me, it's great to be here.

# Pass 2 (add spk:1=Guest)
[Host]     Hello everyone, welcome to the show.
[Guest]    Today we have a special guest with us.
[spk:2]    Thanks for having me, it's great to be here.

# Pass 3 (add spk:2=Interpreter) — done, no spk: left
[Host]       Hello everyone, welcome to the show.
[Guest]      Today we have a special guest with us.
[Interpreter] Thanks for having me, it's great to be here.
```

Tip: each pass is a no-API re-render — the wrapper reads `<base>.diarized.transcript.json` (kept by default) and rewrites `.diarized.srt` from it, so iterating is fast and free.

Note: `--speakers` is ignored when `--diarized-srt-file` is disabled (`--diarized-srt-file=off`). By default, speaker diarization is enabled (`--diarized-srt-file auto`).
