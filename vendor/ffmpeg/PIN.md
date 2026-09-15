# Pinned ffmpeg build

|            |                                                                        |
|------------|------------------------------------------------------------------------|
| Version    | `8.1.2` (release build, not a git-master snapshot)                     |
| Source     | gyan.dev, `ffmpeg-8.1.2-essentials_build.zip`                          |
| URL        | https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip |
| SHA256     | `DB580001CAA24AC104C8CB856CD113A87B0A443F7BDF47D8C12B1D740584A2EC`     |
| License    | **GPL v3** (see `LICENSE`)                                             |

## Why gyan.dev and not BtbN

Either source is allowed. BtbN publishes under a rolling `latest` tag that is
rebuilt daily, and its per-branch assets (`ffmpeg-n9.0-latest-…`) also move. There
is no stable URL for a specific build, which defeats "version-pin it". gyan.dev
publishes immutable, version-numbered packages under `builds/packages/`, so the
URL above will return the same bytes indefinitely.

## Why the binary is not committed

`ffmpeg.exe` is 97 MB. Committing it puts 97 MB in git history permanently, and
every future version bump adds another 97 MB that can never be removed without a
history rewrite. `tools/fetch-ffmpeg.ps1` downloads the exact pinned build and
verifies the SHA256 above, so reproducibility is preserved without the blob.

The binary still lands at `vendor/ffmpeg/ffmpeg.exe`, which is where the build
script and the app look for it; only its storage in git differs.

## Licensing: the GPL is accepted

This build is **GPL v3**, because it contains `libx264`. Every prebuilt Windows
ffmpeg with x264 is GPL; there is no permissive option that also does software
H.264.

**Hudson's decision, 8 Sep 2026: bundle it and accept the GPL.**

The deciding constraint is that Wer has to work on a borrowed machine. That
rules out the alternatives:

- **An LGPL ffmpeg build** (no x264) would leave encoding to `h264_nvenc` /
  `h264_qsv` / `h264_amf` with **no software fallback**. A borrowed theatre
  laptop with no usable hardware encoder could not record at all -- and that is
  precisely the machine that has to work.
- **Not bundling ffmpeg** and detecting a system install fails on exactly that
  machine: a strange Windows laptop with nothing installed on it.

Bundling the GPL build is the only option where recording works everywhere.

### What that obliges us to do

Wer invokes `ffmpeg.exe` as a **separate process over a pipe** -- it does not
link against any ffmpeg library. That used to be load-bearing: it was the
argument that Wer's own source is not a derivative work of ffmpeg. Since
2026-09-11 Wer is itself GPL v3 or later (Hudco Lighting LLC; see `LICENSE`
and `THIRD-PARTY-NOTICES.md`), so the argument no longer has to carry anything,
and the obligations below are the whole of it:

1. **Ship the licence.** `vendor/ffmpeg/LICENSE` is bundled into the exe and
   surfaced in the app under Help → Licences, alongside Wer's own notice.
2. **Say the binary is unmodified**, and name the exact upstream build. The URL
   and SHA256 at the top of this file are that statement.
3. **Be able to honour a source request.** FFmpeg 8.1.2 source is at
   <https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz>; the build configuration
   and the libraries linked in are recorded in `README.txt` beside this file,
   which ships with the exe.

If a closed commercial edition is ever sold, revisit this -- that edition could
not bundle this build at all, the separate-process argument gets tested
properly, and it is a lawyer's question rather than an engineer's. For what is
shipped today, all of it GPL v3, this is settled.
