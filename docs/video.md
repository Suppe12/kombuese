# Recipes from cooking videos

Paste a TikTok, Instagram, YouTube (Shorts), Facebook, Vimeo or Pinterest link into **New → Recipe from a link** and
Kombüse builds a recipe from the video, like it does for recipe pages: with ingredients, steps, nutrition (estimated
where the video says nothing), a meal prep note and a still from the video as the recipe photo.

The video import is part of Kombüse and switched on by default. It needs AI runs (Settings → AI, inside the app or an
outside worker over MCP) and, for the spoken words, a Whisper service.

## How it works

1. The AI run of the link import sees a video link and calls the tool **`read_video(url)`**. The app (not the AI) does
   the rest; agents never talk to Whisper themselves.
2. **yt-dlp** reads the metadata (title, uploader, description, duration, cover image) and downloads the video, at
   most 720p, at most 10 minutes and 200 MB (both can be changed).
3. **ffmpeg** extracts the sound (mono, 16 kHz) and up to 12 stills spread over the video (640 px on the longer side).
4. The sound goes to **Whisper**, which writes down what is said, in the spoken language (detected automatically, or
   fixed in the settings).
5. The tool answers with description, transcript, uploader and link as text and the stills as images (when the AI
   model can see images; otherwise it works with the text). The AI builds the recipe: `quelle` is the video link, the
   tag and a note "From video" name the uploader and what was guessed. The best still (or the cover image) becomes
   the recipe photo with a note "Photo: still from the video by …".
6. Temporary files are deleted right away; stills and cover are kept for 24 hours for the recipe photo, then deleted.

If Whisper is down or not set up, or the video has no speech, the description and the stills still come back, and
many videos have the recipe in their description anyway. Videos that are too long are not downloaded; their
description is still read.

The same tool is offered to outside workers over the MCP server (`/mcp`, see [ai.md](ai.md)); the `link` prompt tells
them to use it. With `frames: false` it answers without images.

## Setup

### With the bundled Whisper (docker-compose.example.yml)

The example compose file has a `whisper` service ([whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice),
faster-whisper, model `small`, CPU) and points the app at it:

```yaml
      - KOMBUESE_WHISPER_URL=http://whisper:9000
      - KOMBUESE_WHISPER_API=asr
```

Nothing else to do. The model (about 500 MB) is downloaded on the first start into `./whisper-cache`. No port is
published: only the app talks to Whisper.

### With a Whisper you already run

Remove the `whisper` service and set the address in **Settings → Video & Whisper** (or with `KOMBUESE_WHISPER_URL`).
Two kinds of API work:

| Kind | Endpoint | Examples |
|---|---|---|
| whisper-asr-webservice | `POST /asr` (field `audio_file`) | `onerahmet/openai-whisper-asr-webservice` |
| OpenAI-compatible | `POST /v1/audio/transcriptions` (fields `file`, `model`) | speaches, faster-whisper-server, LocalAI, the OpenAI API |

"Detect automatically" looks at the service's `/openapi.json`; an address ending in `/v1` is taken as OpenAI-compatible.
The model name matters only for the OpenAI-compatible API (default `whisper-1`; speaches wants e.g.
`Systran/faster-whisper-small`). The app always sends MP3 (or WAV), so services that cannot read MP4 work too.

### With the OpenAI API

Address `https://api.openai.com/v1`, kind OpenAI-compatible, model `whisper-1` (or `gpt-4o-mini-transcribe`), your API
key. The key is stored encrypted and never sent back to the browser. A 10-minute video costs a few cents.

### Test

**Settings → Video & Whisper → Test** sends a built-in 2-second test sound to Whisper and shows which API answered and
how long it took (the text of a test tone is meaningless, often empty).

## Settings

Settings → Video & Whisper (admins), saved per household; the environment variables are the defaults:

| Setting | Variable | Default |
|---|---|---|
| Video import on/off | `KOMBUESE_VIDEO` (`off` switches it off) | on |
| Whisper address | `KOMBUESE_WHISPER_URL` | none (no transcript) |
| Kind of API | `KOMBUESE_WHISPER_API` (`auto`, `asr`, `openai`) | auto |
| Model (OpenAI-compatible) | `KOMBUESE_WHISPER_MODEL` | `whisper-1` |
| API key | `KOMBUESE_WHISPER_KEY` (or in the app, encrypted) | none |
| Spoken language | (app only: automatic, household language or a code) | automatic |
| Longest video | `KOMBUESE_VIDEO_MAX_MINUTES` (1–60) | 10 |
| Largest download | `KOMBUESE_VIDEO_MAX_MB` (10–2000) | 200 |
| Update yt-dlp when a download fails | `KOMBUESE_YTDLP_AUTOUPDATE` | true |
| yt-dlp / ffmpeg command | `KOMBUESE_YTDLP`, `KOMBUESE_FFMPEG` | from the image |

An empty Whisper address in the app falls back to `KOMBUESE_WHISPER_URL`.

## Resources

- Kombüse container: ffmpeg, yt-dlp and QuickJS make the image about 250 MB larger. While a video is read, ffmpeg
  needs up to about 200 MB of RAM; the example sets `mem_limit: 512m`. At most two videos are processed at a time.
- Whisper `small` on the CPU: about 1 GB of RAM; a 1-minute video takes roughly 20–60 seconds on a small server.
  `base` is faster and less exact, `medium` better and about three times slower.
- Disk: the download lives in the temporary directory only while it is processed (at most the size limit).

## yt-dlp updates

Video sites change often, and then yt-dlp needs an update. The image pins a version (build argument
`YTDLP_VERSION` in the Dockerfile). On top of that:

- When a download fails (not for login walls or private videos), the app updates yt-dlp by itself, at most every
  12 hours: `pip install -U "yt-dlp[default]"` from PyPI into `<data>/tools/yt-dlp`, without root. That copy is used
  from then on, also after a restart. Switch it off in the settings or with `KOMBUESE_YTDLP_AUTOUPDATE=false` if the
  container should not install anything from the internet.
- **Settings → Video & Whisper → Update yt-dlp** does the same at once and shows the version.
- Or build the image with a newer pin: `docker compose build --build-arg YTDLP_VERSION=2026.12.01`.
- To go back to the image's version, delete `<data>/tools/yt-dlp`.

YouTube needs a JavaScript runtime for yt-dlp; the image brings the small QuickJS (`qjs`). If YouTube downloads fail
with "JS challenge" errors, update yt-dlp first.

## Security and privacy

- **Where data goes:** the sound only to the Whisper service you set up; the stills, description and transcript only
  to your AI provider (the same as for every other AI run). Nothing else leaves the server, except that yt-dlp
  contacts the video site.
- **SSRF:** links must be http(s) and point to public addresses (`netsafe`, like every other fetch of the app; lifted by
  `KOMBUESE_ALLOW_PRIVATE_URLS=true`). yt-dlp is its own program with its own networking, so the app cannot check
  every connection it makes. To keep it to video sites, the app switches off yt-dlp's generic extractor (which would
  read any web page), checks the download address yt-dlp chose before downloading it, and passes `--ignore-config`.
  Redirects inside yt-dlp's own requests are not checked; if that matters to you, run the container in a network
  without access to your LAN.
- The Whisper address itself is not checked (it is usually in your own network), like the search URL of the AI.
- **Terms of service:** downloading videos may break the terms of the platform (TikTok, Instagram, YouTube …). Kombüse
  downloads only what a member of the household asked for, for private use, and keeps nothing but the recipe and one
  still with a credit to the uploader. Whether that is allowed where you live and on that platform is your
  responsibility; switch the video import off if in doubt.

## Troubleshooting

| Message | What to do |
|---|---|
| "the site wants a login …" | Instagram (and sometimes Facebook) often refuse downloads without a login. Use the share link of a public post, try again later, or paste the recipe text as the hint, or upload screenshots as a photo import. Kombüse does not take cookies or logins for these sites. |
| "the video is private, deleted or not available in this region" | Nothing to do from the app. |
| "yt-dlp could not read the video …" | Update yt-dlp (settings), then try again. |
| "Whisper not reachable at …" | Is the Whisper container running? From the app container: `wget -qO- http://whisper:9000/docs`. The recipe is still made from description and stills. |
| "Whisper answered HTTP 404" | Wrong kind of API or address (e.g. `/v1` missing for an OpenAI-compatible service). Set the kind by hand. |
| "the transcript is empty" | The video has only music. Normal for many short videos. |
| "not downloaded: the video is … min long" | Raise the limit in the settings, if your Whisper is fast enough. |
| "yt-dlp is not installed" | You run the app outside the Docker image: `pip install "yt-dlp[default]"` and install ffmpeg. |
