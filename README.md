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
   Chrome/Chromium opens F1's identity provider directly. Complete any account
   verification in that window. Python handles the OAuth callback and token
   exchange; the F1 account application and its loading screen are not needed.
   The browser closes when authorization is received. The status bar shows the
   remaining session authorization step.
2. On **Download**, paste your video URL and click **Load video**. Choose one or
   more feeds from the table using Ctrl-click or Shift-click, or click **Select all**.
   Channel listing also works before signing in.
3. Select quality and audio. **Check streams** discovers the actual resolutions
   and languages when a single feed is selected. Settings apply to every selected
   feed. Extra international audio is optional.
4. Click **Choose folder**, select MP4 or TS, and click **Download video** (or
   **Download selected** for multiple feeds). Feeds download one at a time into
   separate files. The table shows each feed's queue status; the app shows progress,
   output size, and download speed. A failed feed does not stop the remaining queue.
   **Cancel** stops the queue and removes the current incomplete video, keeping
   completed files. **Open folder** shows a finished file.

**Settings** provides audio offset, sample duration, network timeout, metadata
country/entitlement, cache location, and an optional installed Chromium path.
**Copy playback link** copies the authorized URL without including it in the log.
Select a single feed to use it. Escaped Unicode names and HTML entities in video
metadata are decoded for display, and the app selects an available Unicode font.
The status and Cancel button remain visible while the tabs scroll in smaller windows.

Sign-in and downloads run in background workers so the window stays responsive.
**Remember sign-in** stores the subscription and entitlement tokens using the same protected cache
as the CLI. Uncheck it to keep a password/browser session in memory only and clear
any previously saved session. Pasted session tokens are never saved. **Sign out**
removes the saved session. Passwords and pasted tokens are cleared from their
input fields after a successful sign-in. Expired sessions prompt you to sign in
again; the desktop app does not retain a password for automatic retries.
If `F1TV_USER` and `F1TV_PASSWORD` (or `F1TV_PASS`) are set, the desktop app can
use that password when its password field is blank and the email matches
`F1TV_USER`. An entered password takes priority.

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

Browser sign-in uses F1's public OAuth client, service discovery, and S256 PKCE.
Python creates fresh state and a private verifier for each attempt, opens the F1
identity provider directly in Chromium, validates the callback state, and exchanges
the one-time code itself. The callback is handled locally; the account website's
JavaScript, cookies, and refresh behavior are not involved.
Complete sign-in within three minutes. Available credentials are filled automatically;
account verification remains interactive. A rejected password is not automatically
resubmitted. A display is
required unless `--headless` is selected. Use `--browser-executable PATH` for an
installed Chromium browser. The default Docker image contains the core client and
FFmpeg, without a browser.

New browser sessions use the website's `WEB_DASH` entitlement endpoint and
`WEB_HLS` playback API with both required tokens, device information, a shared
session ID, and a fresh correlation ID for each request. The actual Chromium
user agent and F1 TV website origin are also used for manifest lookup, FFprobe,
and FFmpeg, including HLS variant playlists, initialization files, and segments.
The requested
feed is selected from the returned playback feeds. Sign out and sign in once
after upgrading to 1.3 to capture this browser context for an existing session.
Legacy entitlement-only sessions remain readable.
Resolution choices come from the authorized manifest; using Chromium for sign-in
alone does not guarantee 4K playback or downloads.

Alternatively, set `F1TV_TOKEN` to the `entitlement_token` cookie from your signed-in
F1 TV session. This is the entitlement token, not the subscription token inside
`login-session`. Treat it like a password. An explicitly supplied token is never
saved to disk or silently replaced with another login.

Password login exchanges the subscription token for an entitlement token. Sessions
are cached atomically at `$XDG_CACHE_HOME/f1tv-dl/session.json`, defaulting to
`~/.cache/f1tv-dl/session.json`, or `%LOCALAPPDATA%\f1tv-dl\session.json` on Windows.
The cache stores bearer tokens and browser request context in JSON, with owner-only
file permissions on POSIX.
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

FFprobe and FFmpeg failures show a safe summary of the underlying cause, such as
a media HTTP status, certificate verification failure, unsupported option, or
invalid media data. Raw diagnostics and signed URLs stay private. An empty
FFprobe result is treated as a failure even if FFprobe exits successfully.

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
graphical sign-in, multiple feed selection, Unicode names, audio settings, queue
completion, cancellation, folder selection, error recovery, and sign-out. Media
integration tests also check the desktop service against real locally generated
streams and FFmpeg progress, including required playback headers and matching
browser identity across two queued downloads. Fragmented MP4 HLS tests also check
browser origin headers on child playlists, initialization files, and segments,
with primary and secondary audio inputs.
Browser-flow tests use real Chromium with intercepted fixture pages and no live
account credentials. Install the `browser` extra and run `playwright install chromium`
to enable them. They cover direct identity-provider authorization,
query and fragment callbacks, popups, safe credential entry, state rejection, and
PKCE code exchange without loading the account application. Public protocol fields
from the supplied Firefox recording are retained in a sanitized fixture; the HAR,
recorded credentials, tokens, and signed media URLs are excluded.
