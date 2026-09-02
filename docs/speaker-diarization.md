# Diarizing Tip (Speaker Labels)

`--diarized-srt-file` produces `.diarized.srt` with raw speaker ids (`spk:0`, `spk:1`, ...). Map them to real names with `--speakers-txt-file`:

```bash
# Pass 0: no speaker file — every cue keeps its raw spk:# tag.
gtw --diarized-srt-file=interview.diarized.srt interview.mp4

# Wrapper logs unmapped speakers and recommends creating a .speakers.txt file:
# WARNING Some speakers are not covered by speaker mapping.
#   Speaker map: spk:0=spk:0 spk:1=spk:1 spk:2=spk:2
#   Unmapped: spk:0, spk:1, spk:2
# WARNING To re-render with names, save the mapping to 'interview.speakers.txt', delete the diarized SRT, and re-run:
#   echo 'spk:0=Name0; spk:1=Name1; spk:2=Name2;' > 'interview.speakers.txt' && rm 'interview.diarized.srt' && gtw interview.mp4
```

### Speaker File Format

Create `interview.speakers.txt` (or `.speakers.txt` in current directory):

```text
# Standard replacement (brackets are preserved)
spk:0=Host
spk:1=Guest

# Or pure string replacement: replace bracketed tag directly
[spk:2]=Interpreter:
```

Speaker mapping operates as pure string replacement against the initial cue tag prefix (`[spk:X] `):
- `spk:0=Host` $\rightarrow$ `[Host] Dialogue`
- `[spk:0]=Host:` $\rightarrow$ `Host: Dialogue`
- `[spk:0] =Host:` $\rightarrow$ `Host:Dialogue` (trailing space before `=` is preserved in target)
- `spk:0]=Host:` $\rightarrow$ `[Host: Dialogue`
- `[spk:0]=Host` $\rightarrow$ `Host Dialogue`

By default, `--speakers-txt-file auto` automatically detects `<stem>.speakers.txt` or `.speakers.txt`.
Then simply delete `interview.diarized.srt` and re-run:
```bash
rm interview.diarized.srt
gtw interview.mp4
```

You can also specify a custom path explicitly:
```bash
gtw --speakers-txt-file my_speakers.txt interview.mp4
```
Or disable speaker mapping lookup:
```bash
gtw --speakers-txt-file off interview.mp4
```

Simulation of the renaming (`.diarized.srt` excerpt):

```
# Before (no speaker mapping): all raw ids
[spk:0] Hello everyone, welcome to the show.
[spk:1] Today we have a special guest with us.
[spk:2] Thanks for having me, it's great to be here.

# After speaker mapping applied
[Host]       Hello everyone, welcome to the show.
[Guest]      Today we have a special guest with us.
[Interpreter] Thanks for having me, it's great to be here.
```

Tip: re-rendering is a no-API step — the wrapper reads `<base>.diarized.transcript.json` (kept by default) and rewrites `.diarized.srt` from it, so iterating is fast and free.

Note: Speaker mapping is ignored when `--diarized-srt-file` is disabled (`--diarized-srt-file=off`). By default, speaker diarization is enabled (`--diarized-srt-file auto`).
