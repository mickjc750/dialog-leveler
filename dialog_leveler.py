#!/usr/bin/env python3
"""
dialog_leveler: reduce movie loudness range so action sits close to dialog level.

Adds a processed copy of the default audio stream as a new (default) track,
keeping the original audio track(s) untouched.

Pipeline
  1. Analysis decode of the default audio stream: stereo downmix (ITU-style, LFE
     dropped), split into three bands: K-weighted (BS.1770), speech band
     (300-3400 Hz) and bass (<150 Hz). Power measured in 10 ms frames.
  2. Level map: K-weighted power, smoothed, in LUFS-like dB.
  3. Dialog level = median level of frames whose energy is speech-band dominant.
     Action level = high percentile of the level map.
  4. Static transfer curve: unity up to dialog+knee, then compression so the
     action level lands at dialog+target. Gain = curve(L) - L.
  5. Gain smoothing: fast attack with look-ahead, slower release, short final
     zero-phase smoothing.
  6. Gain applied identically to all channels of the original stream, followed
     by a linked look-ahead peak limiter, encoded and muxed in as a new track.

Needs only python3, numpy and ffmpeg/ffprobe. matplotlib is optional (--plot).
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

FPS = 100  # analysis frames per second (10 ms)


# --------------------------------------------------------------------------- probing

def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def pick_audio(info, index=None):
    """Return (n-th audio stream number, stream dict) for the default audio stream."""
    audio = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if not audio:
        sys.exit("No audio streams found.")
    if index is not None:
        return index, audio[index]
    for i, s in enumerate(audio):
        if s.get("disposition", {}).get("default"):
            return i, s
    return 0, audio[0]


def layout_of(stream):
    lay = stream.get("channel_layout")
    if lay and lay != "unknown":
        return lay
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(int(stream["channels"]), None)


# --------------------------------------------------------------------------- analysis

KWEIGHT = "highshelf=f=1681:g=4:t=q:w=0.707,highpass=f=38:t=q:w=0.5"
SPEECH = "highpass=f=300,highpass=f=300,lowpass=f=3400,lowpass=f=3400"
BASS = "lowpass=f=150,lowpass=f=150"


def analyse(path, a_idx, sr):
    """Return per-frame power arrays (kweighted, speech, bass), each summed over L+R."""
    frame = sr // FPS
    fc = (f"[0:a:{a_idx}]aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=3[a][b][c];"
          f"[a]{KWEIGHT}[ka];[b]{SPEECH}[sb];[c]{BASS}[lb];"
          f"[ka][sb][lb]amerge=inputs=3,aformat=sample_fmts=flt[out]")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-filter_complex", fc,
           "-map", "[out]", "-f", "f32le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    nch = 6
    fbytes = frame * nch * 4
    chunk = fbytes * FPS * 10
    out, rem = [], b""
    while True:
        buf = p.stdout.read(chunk)
        if not buf:
            break
        buf = rem + buf
        n = len(buf) // fbytes * fbytes
        rem = buf[n:]
        x = np.frombuffer(buf[:n], dtype=np.float32).reshape(-1, frame, nch).astype(np.float64)
        ms = (x * x).mean(axis=1)  # frames x 6
        out.append(np.stack([ms[:, 0] + ms[:, 1], ms[:, 2] + ms[:, 3], ms[:, 4] + ms[:, 5]], 1))
    if p.wait() != 0:
        sys.exit("ffmpeg analysis decode failed.")
    a = np.concatenate(out)
    return a[:, 0], a[:, 1], a[:, 2]


def boxcar(x, n):
    n = max(1, int(n))
    if n == 1:
        return x.copy()
    c = np.concatenate([[0.0], np.cumsum(x)])
    half = n // 2
    idx = np.arange(len(x))
    lo = np.clip(idx - half, 0, len(x))
    hi = np.clip(idx - half + n, 0, len(x))
    return (c[hi] - c[lo]) / np.maximum(hi - lo, 1)


def to_db(p):
    return 10 * np.log10(p + 1e-12) - 0.691


def find_levels(pk, ps, pb, args):
    win = round(args.level_window * FPS / 1000)
    sk, ss, sb = (boxcar(x, 40) + 1e-12 for x in (pk, ps, pb))  # 400 ms for classification
    Lc = to_db(sk)
    speech_share = 10 * np.log10(ss / sk)
    bass_ratio = 10 * np.log10(sb / ss)
    active = Lc > args.gate
    speechy = active & (speech_share > -2.5) & (bass_ratio < -6)
    if args.detected_dialog is not None:
        dialog = args.detected_dialog
        how = "given with --detected-dialog"
    elif speechy.mean() > 0.02:
        dialog = float(np.median(Lc[speechy]))
        how = f"median of speech-like frames ({speechy.mean() * 100:.1f}% of runtime)"
    else:
        h, e = np.histogram(Lc[active], bins=np.arange(args.gate, 0, 1))
        dialog = float(e[np.argmax(h)] + 0.5)
        how = "histogram mode (few speech-like frames found)"
    action = float(np.percentile(Lc[active], args.action_pct))
    L = to_db(boxcar(pk, win))
    return dict(L=L, Lc=Lc, speechy=speechy, active=active,
                dialog=dialog, action=action, dialog_how=how)


# --------------------------------------------------------------------------- gain

def transfer_params(dialog, action, args):
    thr = dialog + args.knee_start
    target = dialog + args.target
    if action <= target:
        ratio = 1.0
    else:
        ratio = (action - thr) / max(target - thr, 0.1)
    ratio = float(np.clip(ratio, 1.0, args.max_ratio))
    return thr, ratio


def curve(L, thr, ratio, knee):
    """Soft-knee downward compressor static curve. Returns output level for input L."""
    over = L - thr
    y = L.copy()
    w = knee / 2
    hi = over > w
    y[hi] = thr + over[hi] / ratio
    mid = (over > -w) & ~hi
    o = over[mid] + w
    y[mid] = L[mid] + (1 / ratio - 1) * o * o / (2 * knee)
    return y


def smooth_gain(g, args):
    """g in dB (<=0). Look-ahead min, instant attack, exponential release, final smoothing."""
    la = max(1, round(args.lookahead * FPS / 1000))
    # forward-looking minimum: gain starts dropping `lookahead` ms before a loud event
    pad = np.concatenate([g, np.full(la, g[-1])])
    gm = np.lib.stride_tricks.sliding_window_view(pad, la + 1).min(axis=1)[:len(g)]
    # release: gain may fall instantly but rises with time constant `release`
    a = np.exp(-1.0 / (args.release * FPS / 1000))
    out = np.empty_like(gm)
    s = gm[0]
    for i, v in enumerate(gm.tolist()):
        s = v if v < s else v + a * (s - v)
        out[i] = s
    # zero-phase final smoothing to remove corners (attack becomes ~smooth/2 ramps)
    return boxcar(boxcar(out, round(args.smooth * FPS / 1000)), round(args.smooth * FPS / 1000))


# --------------------------------------------------------------------------- encoding

def choose_codec(stream, args):
    src = stream.get("codec_name")
    ch = int(stream["channels"])
    br = int(stream.get("bit_rate") or 0)
    if args.codec:
        codec = args.codec
    elif src in ("ac3", "eac3", "flac", "opus", "aac"):
        codec = {"opus": "libopus"}.get(src, src)
    elif ch <= 6:
        codec = "eac3"
    else:
        codec = "libopus"
    if codec == "ac3" and ch > 6:
        codec = "eac3" if ch <= 6 else "libopus"
    if codec == "eac3" and ch > 6:
        codec = "libopus"
    if args.bitrate:
        bitrate = args.bitrate
    elif codec == "flac":
        bitrate = None
    elif codec == "ac3":
        bitrate = f"{min(640, max(448, br // 1000))}k"
    elif codec == "eac3":
        bitrate = f"{min(1024, max(640, br // 1000))}k" if ch > 2 else "256k"
    elif codec == "libopus":
        bitrate = f"{min(512, 96 * ch)}k"
    else:  # aac
        bitrate = f"{min(640, 128 * ch)}k"
    return codec, bitrate


def raw_layout_args(layout):
    # ffmpeg >= 5.1 uses -ch_layout, older uses -channel_layout
    v = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=f32le"],
                       capture_output=True, text=True).stdout
    return ["-ch_layout" if "ch_layout" in v else "-channel_layout", layout]


def process_audio(path, a_idx, stream, gain_db, args, tmp_out):
    sr = int(stream["sample_rate"])
    ch = int(stream["channels"])
    layout = layout_of(stream)
    codec, bitrate = choose_codec(stream, args)
    frame = sr // FPS
    t_frames = (np.arange(len(gain_db)) + 0.5) * frame
    lin = 10 ** ((gain_db + args.makeup) / 20)

    dec = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:a:{a_idx}",
         "-f", "f32le", "-acodec", "pcm_f32le", "-"], stdout=subprocess.PIPE)
    lim = 10 ** (args.ceiling / 20)
    af = f"alimiter=limit={lim:.4f}:attack=5:release=80:level=0:latency=1"
    enc_cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr),
               "-ac", str(ch)]
    if layout:
        enc_cmd += raw_layout_args(layout)
    enc_cmd += ["-i", "-", "-af", af, "-c:a", codec]
    if bitrate:
        enc_cmd += ["-b:a", bitrate]
    if codec == "libopus" and ch > 2:
        enc_cmd += ["-mapping_family", "1"]
    enc_cmd += [tmp_out]
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)

    step = sr * 10 * ch * 4  # 10 s
    pos, rem = 0, b""
    while True:
        buf = dec.stdout.read(step)
        if not buf:
            break
        buf = rem + buf
        n = len(buf) // (ch * 4) * (ch * 4)
        rem = buf[n:]
        x = np.frombuffer(buf[:n], dtype=np.float32).reshape(-1, ch)
        idx = np.arange(pos, pos + len(x))
        g = np.interp(idx, t_frames, lin).astype(np.float32)
        enc.stdin.write((x * g[:, None]).tobytes())
        pos += len(x)
    enc.stdin.close()
    if dec.wait() != 0 or enc.wait() != 0:
        sys.exit("ffmpeg decode/encode failed.")
    return codec, bitrate


def remux(path, info, a_idx, stream, tmp_audio, out_path, title):
    n_audio = sum(1 for s in info["streams"] if s.get("codec_type") == "audio")
    start = float(stream.get("start_time") or 0)
    lang = stream.get("tags", {}).get("language")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path]
    if abs(start) > 1e-4:
        cmd += ["-itsoffset", f"{start:.6f}"]
    cmd += ["-i", tmp_audio, "-map", "0", "-map", "1:a", "-c", "copy",
            "-map_metadata", "0", "-map_chapters", "0"]
    for i in range(n_audio):
        cmd += [f"-disposition:a:{i}", "0"]
    cmd += [f"-disposition:a:{n_audio}", "default",
            f"-metadata:s:a:{n_audio}", f"title={title}"]
    if lang:
        cmd += [f"-metadata:s:a:{n_audio}", f"language={lang}"]
    cmd += [out_path]
    if subprocess.run(cmd).returncode != 0:
        sys.exit("ffmpeg remux failed.")


# --------------------------------------------------------------------------- report

def text_hist(Lc, active, speechy, lo=-60, hi=-10):
    lines = []
    ha, e = np.histogram(Lc[active], bins=np.arange(lo, hi + 1, 2))
    hs, _ = np.histogram(Lc[speechy], bins=e)
    m = max(ha.max(), 1)
    for a, s, b in zip(ha, hs, e[:-1]):
        lines.append(f"{b:5.0f} {'#' * int(50 * a / m):<50} {'*' * int(50 * s / m)}")
    return "\n".join(lines)


def plot(res, gain, thr, ratio, args, png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed, skipping plot")
        return
    fig, ax = plt.subplots(3, 1, figsize=(14, 11))
    e = np.arange(args.gate, 0, 1)
    ax[0].hist(res["Lc"][res["active"]], bins=e, alpha=.6, label="all")
    ax[0].hist(res["Lc"][res["speechy"]], bins=e, alpha=.6, label="speech-like")
    ax[0].axvline(res["dialog"], c="g", label="dialog")
    ax[0].axvline(res["action"], c="r", label="action")
    ax[0].set_xlabel("level (LUFS, 400 ms)"); ax[0].legend()
    x = np.linspace(args.gate, 0, 300)
    ax[1].plot(x, curve(x, thr, ratio, args.knee) + args.makeup); ax[1].plot(x, x, ":")
    ax[1].set_xlabel("input level"); ax[1].set_ylabel("output level"); ax[1].grid()
    t = np.arange(len(gain)) / FPS / 60
    ax[2].plot(t, res["L"], lw=.3, label="level")
    ax[2].plot(t, res["L"] + gain, lw=.3, label="level after gain")
    ax[2].plot(t, gain, lw=.5, label="gain dB")
    ax[2].set_xlabel("minutes"); ax[2].legend(); ax[2].grid()
    fig.tight_layout(); fig.savefig(png, dpi=110)
    print("plot:", png)


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="video file (any container/codec ffmpeg can read)")
    ap.add_argument("-o", "--output", help="output file (default: <input>.dialog-leveled.<ext>)")
    ap.add_argument("--stream", type=int, help="audio stream number (default: default stream)")
    ap.add_argument("--target", type=float, default=6.0, help="loud content ends this many dB above dialog (6)")
    ap.add_argument("--knee-start", type=float, default=2.0, help="compression starts this many dB above dialog (2)")
    ap.add_argument("--knee", type=float, default=6.0, help="soft knee width dB (6)")
    ap.add_argument("--max-ratio", type=float, default=20.0, help="upper limit on the compression ratio (20)")
    ap.add_argument("--action-pct", type=float, default=99.0, help="percentile defining action level (99)")
    ap.add_argument("--dialog-level", type=float,
                    help="set the output dialog level in LUFS, e.g. -27 (default: leave dialog at its original level)")
    ap.add_argument("--detected-dialog", type=float,
                    help="skip dialog detection and use this input dialog level (LUFS)")
    ap.add_argument("--gate", type=float, default=-60.0, help="ignore levels below this LUFS, never adjusted (-60)")
    ap.add_argument("--level-window", type=float, default=200.0, help="level detector window ms (200)")
    ap.add_argument("--lookahead", type=float, default=50.0, help="gain starts falling this many ms before a loud event (50)")
    ap.add_argument("--release", type=float, default=800.0, help="release time constant ms (800)")
    ap.add_argument("--smooth", type=float, default=40.0, help="final gain smoothing ms (40)")
    ap.add_argument("--ceiling", type=float, default=-1.0, help="limiter ceiling dBFS (-1)")
    ap.add_argument("--codec", help="output audio codec (default: same as source where possible, else eac3/libopus)")
    ap.add_argument("--bitrate", help="output audio bitrate, e.g. 640k")
    ap.add_argument("--title", default="Dialog leveled", help="title of the new audio track")
    ap.add_argument("--analyse-only", action="store_true", help="print analysis, write nothing")
    ap.add_argument("--plot", action="store_true", help="save a PNG of histogram, curve and gain")
    args = ap.parse_args()

    t0 = time.time()
    info = probe(args.input)
    a_idx, st = pick_audio(info, args.stream)
    sr = int(st["sample_rate"])
    print(f"audio stream a:{a_idx}: {st.get('codec_name')} {layout_of(st)} {sr} Hz "
          f"lang={st.get('tags', {}).get('language', '?')}")
    if sr % FPS:
        sys.exit(f"sample rate {sr} not a multiple of {FPS}")

    pk, ps, pb = analyse(args.input, a_idx, sr)
    res = find_levels(pk, ps, pb, args)
    thr, ratio = transfer_params(res["dialog"], res["action"], args)
    L = res["L"]
    g = curve(L, thr, ratio, args.knee) - L
    g[L < args.gate] = 0.0
    gain = smooth_gain(g, args)
    args.makeup = 0.0 if args.dialog_level is None else args.dialog_level - res["dialog"]

    act = res["active"]
    after = L + gain + args.makeup
    print(f"analysed {len(L) / FPS / 60:.1f} min in {time.time() - t0:.0f} s")
    print(f"dialog level   {res['dialog']:6.1f} LUFS  ({res['dialog_how']})")
    print(f"action level   {res['action']:6.1f} LUFS  (p{args.action_pct:g}), "
          f"{res['action'] - res['dialog']:+.1f} dB vs dialog")
    print(f"curve          threshold {thr:.1f}, ratio {ratio:.1f}:1, knee {args.knee:g} dB")
    print(f"after          p{args.action_pct:g} {np.percentile(after[act], args.action_pct):6.1f} LUFS "
          f"({np.percentile(after[act], args.action_pct) - res['dialog'] - args.makeup:+.1f} dB vs dialog), "
          f"max gain reduction {-gain.min():.1f} dB, "
          f"reducing >1 dB {np.mean(gain < -1) * 100:.1f}% of runtime")
    if args.dialog_level is not None:
        print(f"output dialog  {args.dialog_level:6.1f} LUFS  (gain {args.makeup:+.1f} dB applied to everything)")
        if args.dialog_level + args.target > -14:
            print(f"warning: loud parts will sit near {args.dialog_level + args.target:.0f} LUFS; "
                  f"the peak limiter will work hard. Consider a lower --dialog-level.")
    print("\nhistogram (# all, * speech-like), LUFS:\n" + text_hist(res["Lc"], act, res["speechy"]))

    base, ext = os.path.splitext(args.input)
    out_path = args.output or f"{base}.dialog-leveled{ext}"
    if args.plot:
        plot(res, gain, thr, ratio, args, os.path.splitext(out_path)[0] + ".png")
    if args.analyse_only:
        return

    tmp = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                       f".tmp-{os.getpid()}-{os.path.basename(base)}.mka")
    try:
        t1 = time.time()
        codec, bitrate = process_audio(args.input, a_idx, st, gain, args, tmp)
        print(f"encoded {codec} {bitrate or ''} in {time.time() - t1:.0f} s")
        remux(args.input, info, a_idx, st, tmp, out_path, args.title)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    print(f"wrote {out_path}  (total {time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
