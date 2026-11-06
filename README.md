# dialog-leveler

Movie soundtracks are often mixed with a huge dynamic range: dialog sits 10–20 dB below explosions and music, so you keep reaching for the remote. `dialog-leveler` fixes that **file by file**. It analyses the whole film, finds the dialog level, and turns down the loud parts so they end up only a few dB above the dialog.

The original audio is never touched. A processed copy is **added as a new, default audio track**, so you can always switch back.

```
$ dialog-leveler movie-file.mkv
audio stream a:0: ac3 stereo 48000 Hz lang=eng
analysed 132.1 min in 31 s
dialog level    -33.7 LUFS  (median of speech-like frames (10.9% of runtime))
action level    -17.1 LUFS  (p99), +16.6 dB vs dialog
curve          threshold -31.7, ratio 3.7:1, knee 6 dB
after          p99  -28.1 LUFS (+5.6 dB vs dialog), max gain reduction 16.2 dB, ...
encoded ac3 640k in 26 s
wrote movie-file.dialog-leveled.mkv  (total 98 s)
```

## Features

- Works with any container and codec ffmpeg can read (MKV, MP4, AC-3, E-AC-3, DTS, TrueHD, AAC, FLAC, Opus…).
- Keeps the channel layout: 5.1 in, 5.1 out. Loudness is *measured* as if downmixed to stereo (LFE dropped, centre and surrounds at −3 dB), which matches what you hear on a TV or stereo.
- Same gain applied to every channel, so the surround image doesn't shift.
- Look-ahead gain with a fast attack and a slow release, plus a final peak limiter: no clipping and minimal pumping.
- Video, subtitles, chapters and other audio tracks are copied untouched (no video re-encode).
- Fast: about 1–2 minutes for a full-length film.

## Installation

Tested on Debian 12/13. Any Linux with Python 3.9+ should work. No virtualenv needed.

**1. Install the dependencies:**

```sh
sudo apt install ffmpeg python3-numpy python3-matplotlib
```

`python3-matplotlib` is optional. It is only used by `--plot`.

**2. Install the script:**

```sh
git clone https://github.com/mickjc750/dialog-leveler.git
cd dialog-leveler
./install.sh
```

This copies the script to `~/.local/bin/dialog-leveler`. If `dialog-leveler` isn't found afterwards, log out and back in, or add `~/.local/bin` to your `PATH`. To install for all users instead, run `sudo ./install.sh /usr/local/bin`.

To update later, run `git pull && ./install.sh`.

You can also run it in place without installing: `python3 dialog_leveler.py movie.mkv`.

## Usage

```sh
dialog-leveler movie.mkv                      # writes movie.dialog-leveled.mkv
dialog-leveler movie.mkv -o out.mkv           # choose output name
dialog-leveler movie.mkv --dialog-level -27   # also set dialog to -27 LUFS
dialog-leveler movie.mkv --analyse-only       # just print the measurements
dialog-leveler movie.mkv --plot               # also save movie.dialog-leveled.png
dialog-leveler --help                         # all options
```

Process a whole folder:

```sh
for f in *.mkv; do dialog-leveler "$f"; done
```

### Common options

| Option | Default | What it does |
|---|---|---|
| `--target` | `6` | How many dB above dialog the loudest parts end up. Lower = flatter, higher = more punch. |
| `--release` | `800` | How fast (ms) the volume recovers after a loud moment. Shorter recovers faster but can pump. |
| `--dialog-level` | unchanged | Output dialog level in LUFS, e.g. `-27`. Everything is shifted so dialog lands here, which gives every film the same volume. Around -27 to -24 is a sensible range; loud parts end up `--target` dB above it. |
| `--detected-dialog` | auto | Skip detection and use this as the input dialog level (LUFS), if auto-detection gets it wrong. |
| `--codec`, `--bitrate` | same as source | Output audio format, e.g. `--codec eac3 --bitrate 640k`. |
| `--stream` | default track | Which audio stream to process (0 = first audio stream). |
| `--title` | `Dialog leveled` | Title of the new audio track. |
| `--no-default` | off | Don't make the new track the default. The original stays selected and you can switch to the leveled track in your player when needed. |

The remaining options (`--knee`, `--knee-start`, `--lookahead`, `--smooth`, `--level-window`, `--action-pct`, `--max-ratio`, `--gate`, `--ceiling`) are for fine-tuning. The defaults suit most films.

### Output audio codec

If the source codec can be encoded by ffmpeg (AC-3, E-AC-3, AAC, Opus, FLAC), the same codec is used at the same or a higher bitrate. Otherwise (DTS, TrueHD, PCM…) the output is E-AC-3 at 640 kb/s or more for up to 5.1 channels, and Opus for 7.1. Dolby Atmos object metadata is not preserved in the new track, but it is still there in the original track.

## How it works

1. **Measure.** The default audio stream is decoded and downmixed to stereo for measurement only. It is filtered three ways: K-weighted (the ITU-R BS.1770 loudness curve), speech band (300–3400 Hz) and bass (<150 Hz). Power is computed in 10 ms frames.
2. **Find dialog and action.** Frames whose energy is dominated by the speech band (and not by bass) are treated as dialog, and their median level is the dialog level. The action level is the 99th percentile of all non-silent frames. On real films the overall loudness histogram is usually one broad hump, not two clean peaks, so the spectral split is what makes detection reliable.
3. **Build the curve.** A soft-knee compressor curve leaves everything up to *dialog + 2 dB* alone, then compresses above that with a ratio chosen so the action level lands at *dialog + target*.
4. **Smooth the gain.** The gain starts falling 50 ms *before* a loud event (look-ahead), drops quickly, and recovers with an 800 ms release, then gets a final short zero-phase smoothing.
5. **Apply.** The gain is applied identically to every channel of the original stream at full sample rate. A look-ahead peak limiter (−1 dBFS ceiling, delay-compensated) follows, and the result is encoded and muxed in as a new default track.

Use `--plot` to see the level histogram, the curve and the gain over time for a file.

## Limitations

- Re-encoding to a lossy codec is a small generation loss (inaudible at the bitrates used).
- The new AC-3/E-AC-3 track can be a few milliseconds behind the original because of encoder delay. That is far below what is noticeable for lip sync.
- Only one audio stream is processed per run (the default one, or `--stream`).

## Credits

This tool was implemented by [Claude Opus 5.5](https://claude.ai), Anthropic's AI model, from a prompt by [@mickjc750](https://github.com/mickjc750) describing the intended method: demux the audio, build a level map from a mono detection signal with zero-phase smoothing, use a level histogram to find the dialog and action levels, derive a gain curve from them, apply it to every track and remux. That design was discussed and refined (K-weighting, measuring as a stereo downmix, speech-band dialog detection, asymmetric attack/release, a final limiter) before any code was written, then tuned on a real film.

## License

MIT
