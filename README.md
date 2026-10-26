# f1tv-dl

A Python desktop and command-line client for listing F1 TV feeds, resolving authorized playback
URLs, and downloading videos with FFmpeg. Rebuilt from the original
[`thedave42/f1tv-dl`](https://github.com/thedave42/f1tv-dl) Node.js tool, preserving
its main command and flags. Requires Python 3.10+. The core package uses only the
standard library; browser login is an optional extra.

## Install

Install [FFmpeg](https://ffmpeg.org/download.html), including `ffprobe`, on PATH.
From this repository:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
f1tv-dl --help
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. You can also run
`python -m f1tv_dl` from the repository without installing the package.

## Desktop app

Open the graphical interface after installing the package:

```sh
f1tv-dl-gui
```

You can also run `python -m f1tv_dl.gui` directly from this repository.
The desktop app uses Tk, included with the standard Windows/macOS Python installer.
On Debian/Ubuntu, install `python3-tk` for the Python version running the app.
A desktop display and FFmpeg are required; the CLI Docker image stays headless.

1. On **Account**, select **Browser sign-in**, **Email and password**, or **Session token**.
   For browser sign-in, click **Install browser support** once, then **Sign in**.
   Complete any account verification in the official F1 TV browser window.
2. On **Download**, paste your video URL and click **Load video**. Choose a feed
   from the table. Channel listing also works before signing in.
3. Select quality and audio. **Check streams** discovers the actual resolutions
   and languages for the chosen feed. Extra international audio is optional.
4. Click **Choose folder**, select MP4 or TS, and click **Download video**.
   The app shows progress, output size, and download speed. **Cancel** stops the
   operation and removes an incomplete video; **Open folder** shows a finished file.

**Settings** provides audio offset, sample duration, network timeout, metadata
country/entitlement, cache location, and an optional installed Chromium path.
**Copy playback link** copies the authorized URL without including it in the log.
The status and Cancel button remain visible while the tabs scroll in smaller windows.

Sign-in and downloads run in background workers so the window stays responsive.
**Remember sign-in** stores only the session token using the same protected cache
as the CLI. Uncheck it to keep a password/browser session in memory only and clear
any previously saved session. Pasted session tokens are never saved. **Sign out**
removes the saved session. Passwords and pasted tokens are cleared from their
input fields after a successful sign-in. Expired sessions prompt you to sign in
again; the desktop app does not retain a password for automatic retries.

F1 TV's subscription, regional, browser verification, and DRM requirements apply
to the desktop app just as they do to the CLI.

## List feeds

Metadata and channel listing require neither login nor FFmpeg:

```sh
f1tv-dl 'https://f1tv.formula1.com/detail/1000010409/2026-united-states-grand-prix' --channel-list
```

Select a feed by case-insensitive title, driver name, racing number, three-letter
code, or channel ID. Exact matches take priority; ambiguous partial names fail
with the matching feed names. The advertised default feed, usually F1 LIVE, is
selected when `--channel` is omitted.

## Sign in

Use your own F1 TV account with access to the requested content. Set `F1TV_USER`
and `F1TV_PASSWORD` in the environment. The old `F1TV_PASS` alias also works.
Missing credentials are requested interactively when a terminal is available;
noninteractive runs fail with instructions instead of hanging. Passwords are never
cached. `.env` files are not loaded automatically; export your variables or use a
Docker `--env-file`.

F1 TV may reject password API login with HTTP 403 or require browser verification.
Use the optional browser integration for the normal sign-in flow:

```sh
python -m pip install '.[browser]'
python -m playwright install chromium
f1tv-dl URL --login-method browser
```

Complete sign-in in the browser window within two minutes. Available credentials
are filled automatically; browser verification remains interactive. A display is
required unless `--headless` is selected. Use `--browser-executable PATH` for an
installed Chromium browser. The default Docker image contains the core client and
FFmpeg, without a browser.

Alternatively, set `F1TV_TOKEN` to the `entitlement_token` cookie from your signed-in
F1 TV session. This is the entitlement token, not the subscription token inside
`login-session`. Treat it like a password. An explicitly supplied token is never
saved to disk or silently replaced with another login.

Password login exchanges the subscription token for an entitlement token. Sessions
are cached atomically at `$XDG_CACHE_HOME/f1tv-dl/session.json`, defaulting to
`~/.cache/f1tv-dl/session.json`, or `%LOCALAPPDATA%\f1tv-dl\session.json` on Windows.
The cache stores a bearer token in JSON, with owner-only file permissions on POSIX.
Protect the directory with your account ACLs on Windows. Use `--session-file PATH`
to choose a location or `--no-cache` to disable cache reads and writes. The old
`.f1tv.json` cache is not imported; sign in once after migrating.

## Download examples

Replace `URL` below with the complete F1 TV detail URL:

```sh
# Download the default feed.
f1tv-dl URL -o ./download

# Select a driver camera.
f1tv-dl URL -c VER

# Download international Dutch commentary at an exact resolution.
f1tv-dl URL -c INTERNATIONAL -a nld -s 1920x1080

# Add international commentary as a second audio track.
f1tv-dl URL -i nld --itsoffset=-00:00:04.750

# Download a short MPEG-TS sample.
f1tv-dl URL --duration 10 -f ts

# Print only the authorized playback URL to stdout.
f1tv-dl URL --stream-url
```

Output filenames use the page slug and the selected channel, if specified.
Existing files are preserved unless `--overwrite` is given. Downloads use a
temporary file in the destination directory; the final file is published only
after FFmpeg succeeds. Failed or cancelled temporary downloads are removed.
Publishing without overwrite uses an atomic hard link, so the destination
filesystem must support hard links, as typical NTFS, ext4, and APFS volumes do.

FFprobe selects the highest resolution and frame rate for `--video-size best`,
or an exact `WIDTHxHEIGHT` match. Audio is mapped by language and its absolute
stream index within the selected HLS program. If a language is unavailable, a
warning identifies the fallback to the default track. International audio adds
one audio track without a second video. Audio offset defaults to zero; adjust it
for the broadcast. MP4 and TS output copy the original codecs without re-encoding.

Clear HLS and DASH and ordinary HLS AES-128 are supported. DRM-protected manifests
are rejected. F1 TV enforces subscription, region, availability, browser
verification, and CDN access. Signed URLs appear only for `--stream-url`; routine
logs suppress them. HTTP 401 refreshes a cached login once; HTTP 403 reports an
access problem without repeatedly submitting credentials.

## Options

Run `f1tv-dl --help` for all options. Original flags remain available: `-c`, `-i`,
`-t`, `-a`, `-s`, `-f`, `-o`, `-U`, `-P`, `--channel-list`, `--stream-url`, and `-l`.

| Option | Default / environment | Purpose |
| --- | --- | --- |
| `--token` | `F1TV_TOKEN` | Existing entitlement token |
| `--login-method` | `password` | `password` or `browser` login |
| `--session-file` | Platform cache | Session location |
| `--no-cache` | Off | Disable session cache |
| `--country` | `F1TV_COUNTRY` or `USA` | Metadata home country |
| `--entitlement` | `F1TV_ENTITLEMENT` or `F1_TV_Pro_Annual` | Metadata entitlement |
| `--timeout` | 30 seconds | Network read timeout |
| `--duration` | Entire video | Output duration in seconds |
| `--overwrite` | Off | Replace output after successful download |
| `-o` | `F1TV_OUTDIR` or current directory | Output directory |

Country and entitlement settings affect metadata; they do not grant access or
change your account region. Standard proxy variables and system CA trust apply
to API requests. Configure the corresponding trust stores for Chromium and FFmpeg
when using an intercepting proxy. TLS verification stays enabled.

Exit codes: `0` success, `1` operational error, `2` invalid arguments, `130` cancelled.

## Docker

```sh
docker build -t f1tv-dl .
mkdir -p download session-cache
docker run --rm --user "$(id -u):$(id -g)" \
  -e F1TV_USER -e F1TV_PASSWORD \
  -v "$PWD/download:/download" -v "$PWD/session-cache:/cache" \
  f1tv-dl 'https://f1tv.formula1.com/detail/1000010409/2026-united-states-grand-prix'
```

The Linux example uses your host UID/GID for writable bind mounts. On Windows,
omit `--user` and use your shell's bind-mount syntax. For proxy builds, add
`--secret id=proxy_ca,src=/path/to/ca-bundle.pem`. Runtime trust must be supplied
separately; session certificates are never baked into image layers.

## Development

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build --outdir work/dist
```

Tests use public metadata fixtures and mocked authentication responses. Integration
tests generate small local HLS/DASH streams, then use real FFprobe and FFmpeg to
check resolution, language, container, secondary audio, and the full CLI flow.
They require loopback networking and skip when FFmpeg is absent. No F1 TV
credentials or external requests are needed in CI.

Desktop interaction tests run when Tk and a display are available. On headless
Linux, use `xvfb-run -a python -m unittest discover -s tests -v`. They exercise
graphical sign-in, feed selection, audio settings, download completion, cancellation,
folder selection, error recovery, and sign-out. Media integration tests also check
the desktop service against real locally generated streams and FFmpeg progress.
