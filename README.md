# tgarr

[![Test, build & push image](https://github.com/omer182/tgarr/actions/workflows/docker.yml/badge.svg)](https://github.com/omer182/tgarr/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**tgarr is a self-hosted bridge between Telegram and the \*arr stack.** It turns the Telegram channels you are a
member of into a source for Sonarr and Radarr: a Newznab indexer, a SABnzbd-compatible download client, and a web
page for picking files by hand. It runs as one Docker container next to Sonarr, Radarr, Prowlarr and Plex.

It is an independent, unofficial project. It is not affiliated with Telegram, Sonarr, Radarr, Prowlarr, Plex or any
channel.

## What it is for

Some shows, especially ones with fan-made subtitles, never reach torrent trackers but are posted in Telegram
channels. tgarr logs in to Telegram as your own account, reads the channels you are in, and makes their files
available to your media stack in two ways:

- **Automatically.** tgarr presents itself to Sonarr as an indexer (Newznab, added through Prowlarr) and as a
  download client (SABnzbd-compatible). You request a show as usual. If your trackers have nothing, Sonarr finds
  it on Telegram, tgarr downloads it, and Sonarr imports it.
- **By hand.** A web page where you pick a channel, see its files with season and episode numbers already worked
  out, and download what you want. Files are imported into Sonarr or Radarr with hardlinks and Plex is refreshed.

Use it only for material you have the right to download.

## Contents

- [How it works](#how-it-works)
- [Example: AnimeIL-TV](#example-animeil-tv)
- [What you need](#what-you-need)
- [Run it with Portainer](#run-it-with-portainer)
- [Connect Sonarr and Prowlarr](#connect-sonarr-and-prowlarr)
- [Using the web page](#using-the-web-page)
- [Joining channels](#joining-channels)
- [Your Telegram account](#your-telegram-account)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## How it works

```
You -> Overseerr -> Sonarr -> Prowlarr -> tgarr (indexer)   answers from an index of your linked channels
                    Sonarr -> tgarr (download client)       downloads from Telegram into /data/telegram/tv
                    Sonarr imports the finished file into the library -> Plex
```

- **Linked channels.** A channel answers Sonarr's searches once it is linked to a series. tgarr links a channel
  by itself when the match is certain: an exact title match, or a series you picked yourself. A guessed match is
  never linked without you confirming it.
- **A cached index.** Each linked channel is scanned in the background and the result is kept in memory and on
  disk. A Sonarr search is answered from that index, so it never triggers a Telegram scan, and tgarr answers
  straight away after a restart.
- **Season and episode numbers.** They are read from the caption first and the file name second, in English and
  Hebrew. A season stated together with its episode ("Season 1 Episode 12") beats a season mentioned on its own,
  and the caption's episode number beats upload artifacts in file names. Trailers, clips and other short or tiny
  files are set aside as extras.
- **Numbering translation.** Channels often count seasons differently from TVDB. tgarr maps the channel's numbers
  onto Sonarr's episodes: as posted when the seasons fit, or counted continuously when a channel season is longer
  than Sonarr's (a channel's 18-episode "season 2" that is really TVDB seasons 2 and 3). Releases offered to Sonarr
  are always named in Sonarr's numbering.
- **Torrents first.** With Sonarr's delay profile set to prefer torrents and delay Usenet, your trackers get a head
  start and Telegram is the fallback. tgarr stamps each release with the time Sonarr first asked for it, so the
  delay counts from your request and not from the date of the Telegram post.
- **Directories.** A "directory" channel that lists one invite link per show gives tgarr English titles for
  channels named in another language. It also powers the Wanted tab: series Sonarr is missing that one of those
  channels carries.

## Example: AnimeIL-TV

[AnimeIL-TV](https://t.me/AnimeILTV) is a Hebrew fansub group. It runs one Telegram channel per show, plus a
directory channel, `@AnimeILTV`, that posts an invite link for each of them with the show's name in English and
Hebrew:

```
Spy X Family - משפחה חשאית
https://t.me/+XXXXXXXXXXXXXXXX
```

The show channels themselves are named and captioned in Hebrew, for example
`[AnimeIL-TV] - משפחה חשאית`, with files like `עונה 1 פרק 12` (season 1, episode 12).

Set up once:

1. Join `@AnimeILTV` with your Telegram account.
2. In tgarr go to Sources -> Directory channels and add `AnimeILTV`. tgarr reads the list of shows and their
   English titles.

Then, for a show such as Spy x Family:

1. Request it in Overseerr (or add it in Sonarr). Sonarr searches your trackers first.
2. It appears in tgarr's Wanted tab, because Sonarr is missing it and the directory lists a channel for it.
3. Join that channel: press "Join & get it now", or open the link and join in Telegram. With hands-off mode on,
   tgarr does this itself when your trackers still have nothing after the fallback delay.
4. tgarr scans the channel, reads `עונה 1 פרק 12` as S01E12 from the captions, links the channel to
   "SPY x FAMILY" through the directory's English title, and offers Sonarr releases named like
   `SPY.x.FAMILY.S01E12.1080p.WEB-DL.HebSub-TG`.
5. Sonarr grabs them, tgarr downloads them, Sonarr imports them, and they show up in Plex.

Channels do not always count seasons the way TVDB does. AnimeIL-TV's Demon Slayer channel posts an 18-episode
"season 2" that TVDB splits into season 2 (7 episodes) and season 3 (11 episodes). tgarr notices that the channel
season is longer than Sonarr's and counts continuously, so the channel's "season 2, episode 8" is offered to Sonarr
as S03E01.

Any channel works as a source, with or without a directory. A directory only makes matching automatic for channels
that are not named in English.

## What you need

- Docker, with Sonarr (and optionally Radarr, Prowlarr, Plex) on a shared Docker network.
- A Telegram account, and API credentials for it from <https://my.telegram.org> -> API development tools.
- The same `/data` mount in tgarr that Sonarr and Radarr use, so that paths match and hardlinks work.

## Run it with Portainer

The image is built by GitHub Actions and published as `ghcr.io/omer182/tgarr:latest`.

1. **Create the config folder** on the host, owned by the user the container runs as:
   ```bash
   sudo mkdir -p /hosted-apps/tgarr/config && sudo chown 1000:1000 /hosted-apps/tgarr/config
   ```
2. **Add the stack.** In Portainer go to Stacks -> Add stack -> Web editor, paste
   [docker-compose.prod.yml](docker-compose.prod.yml), and adjust the paths and network name if yours differ.
3. **Set the stack's environment variables** (listed in [.env.example](.env.example)): `TG_API_ID`,
   `TG_API_HASH`, `SONARR_KEY`, `RADARR_KEY` and optionally `PLEX_TOKEN`.
4. **Deploy the stack**, then log in to Telegram once. It asks for your phone number, the code Telegram sends you,
   and your two-step password if you have one:
   ```bash
   docker exec -it tgarr python -m app.login
   ```
   ```bash
   docker restart tgarr
   ```
5. Open `http://<server>:8765`. The dots in the header show whether Telegram, Sonarr, Radarr and Plex are reachable.

To update, use "Pull and redeploy" on the stack in Portainer.

### Add it to an existing stack

If Sonarr, Radarr and the rest already live in one stack, add tgarr as one more service instead of a separate
stack. Copy this block into the stack's `services:` section, next to Sonarr, and replace the five `<...>`
placeholders with your values (keep the quotes):

```yaml
  tgarr:
    image: ghcr.io/omer182/tgarr:latest
    container_name: tgarr
    user: "1000:1000"
    restart: unless-stopped
    ports:
      - 8765:8765
    networks: [proxy]
    environment:
      TG_API_ID: "<TG_API_ID>"
      TG_API_HASH: "<TG_API_HASH>"
      SONARR_URL: http://sonarr:8989
      SONARR_KEY: "<SONARR_KEY>"
      RADARR_URL: http://radarr:7878
      RADARR_KEY: "<RADARR_KEY>"
      PLEX_URL: http://172.18.0.1:32400
      PLEX_TOKEN: "<PLEX_TOKEN>"
      TZ: Asia/Jerusalem
    volumes:
      - /hosted-apps/tgarr/config:/config
      - /mnt/storage/data:/data
```

What to adjust for your own setup:

- `networks`: the Docker network your Sonarr and Prowlarr containers are on, so they can reach `http://tgarr:8765`.
- `SONARR_URL`, `RADARR_URL`: your containers' names and ports on that network.
- `PLEX_URL`: where Plex is reachable from inside the container. `172.18.0.1` is the gateway of the network above,
  which works when Plex runs on the host network. Leave `PLEX_URL` and `PLEX_TOKEN` out if you do not use Plex.
- `user`: the user id that owns the config folder and can write the `/data` folder.
- The second volume must be the same `/data` mount Sonarr and Radarr use.

Then create the config folder (step 1 above), update the stack, and log in to Telegram (step 4 above).

## Connect Sonarr and Prowlarr

The exact values are shown in the web page under Sources -> Sonarr integration -> Show connection details.

| Where | What to add | Settings |
|---|---|---|
| Prowlarr -> Indexers | Generic Newznab | URL `http://tgarr:8765/newznab`, API path `/api`, tgarr's API key, a low priority (for example 50) |
| Sonarr -> Download Clients | SABnzbd | Host `tgarr`, port `8765`, URL base `sab`, the same API key, category `tv` |
| Sonarr -> Profiles -> Delay Profiles | edit the default | Prefer Torrent, Usenet delay 30 minutes, "bypass if highest quality" off |

Prowlarr syncs the indexer to Sonarr. Sonarr treats tgarr as a Usenet indexer and client; that is only the
protocol it speaks to them, nothing is fetched from Usenet.

Movies through Radarr work in the manual flow. The indexer and download client currently serve TV only.

## Using the web page

- **Find.** Choose a source to scan it. tgarr suggests the matching series, shows where each file will land in
  Sonarr's numbering, marks what is already in your library, and lets you select everything that is missing.
  Guessed numbers and duplicates are highlighted so you can check them.
- **Wanted.** Series Sonarr is missing episodes for that one of your directory channels carries, with what will
  happen next for each.
- **Sources.** The channels tgarr reads and the series each is linked to. Add a channel by pasting its link or by
  picking it from your Telegram chats. This tab also holds your directory channels, the Sonarr integration status
  and the switch for joining channels automatically.
- **Jobs.** Downloads grouped per request, with pause, resume, stop and remove. Downloads that Sonarr started are
  tagged "via Sonarr".

## Joining channels

tgarr can only see files in channels your Telegram account has joined.

- **By default you join.** Use the "Join & get it now" button in the Wanted tab, or join the channel in the
  Telegram app. tgarr notices within a few minutes, links the channel, and asks Sonarr to search again once the
  fallback delay since your request has passed.
- **Optional: let tgarr join.** Switch on "Hands-off mode" on the Sources page. When a requested show is still
  missing after the fallback delay and Sonarr has nothing queued for it, tgarr joins that show's directory channel
  itself. It only joins channels listed in your directories, only for shows requested in the last week, and at
  most 5 a day.

## Your Telegram account

tgarr is not a bot. A bot cannot see channels it has not been added to and cannot download large files, so tgarr
logs in as a regular user: you.

- Everything it does on Telegram is done by your account: reading channels, downloading, and joining if you allow
  it. It never sends messages.
- **The session file is a full login.** `/config/tgarr.session` gives complete access to the account. Keep the
  config folder private and out of any repository. You can revoke the session in Telegram under Settings ->
  Devices.
- **The web page has no login.** Anyone who can reach port 8765 can use it. Keep it on your home network and do
  not publish it through a reverse proxy.
- Telegram may limit accounts it considers automated. tgarr is read-only and conservative, but if you would rather
  not involve your main account, log in with a second Telegram account instead. It needs its own phone number and
  has to be a member of the channels.

## Configuration

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | | Telegram API credentials (required) |
| `SONARR_URL`, `SONARR_KEY` | `http://sonarr:8989` | Sonarr address and API key |
| `SONARR_ROOT`, `SONARR_PROFILE` | `/data/media/tv`, `6` | Root folder and quality profile id for series tgarr adds itself |
| `RADARR_URL`, `RADARR_KEY` | `http://radarr:7878` | Radarr address and API key |
| `RADARR_ROOT`, `RADARR_PROFILE` | `/data/media/movies`, `6` | Root folder and quality profile id for movies tgarr adds itself |
| `PLEX_URL`, `PLEX_TOKEN` | | Plex address and token (optional) |
| `PLEX_PATH_MAP` | `/data/media=/data/data/media` | Sonarr's path prefix = Plex's path prefix |
| `INDEX_REFRESH` | `600` | Seconds between channel index refreshes |
| `WORKERS` | `2` | Downloads running at once |
| `SCAN_LIMIT` | `3000` | Most recent messages read per channel |

Everything tgarr remembers lives in `/config`: the Telegram session, sources, jobs, the index, settings and the
API key for Sonarr and Prowlarr.

Folders under `/data`:

| Folder | Used for |
|---|---|
| `torrents/tv`, `torrents/movies` | Manual downloads, then hardlinked into the library by Sonarr or Radarr |
| `telegram/tv` | Downloads Sonarr requested; Sonarr moves them into the library |

## Troubleshooting

- **Sonarr finds nothing from tgarr.** Check Sources: the channel must be linked to the series. Then check
  Sonarr -> System -> Status. If it says "Indexers unavailable due to failures: tgarr", tgarr was unreachable for
  a moment (for example during a restart) and Sonarr and Prowlarr are skipping it. Press Test on the indexer in
  both to clear that.
- **An anime season search takes minutes.** For series of type Anime, Sonarr asks about every episode separately
  under several spellings of the title, a couple of seconds apart, on every indexer. tgarr answers each one
  immediately. A reverse proxy in front of Sonarr may time out before a long interactive search returns; search
  from Sonarr's local address, or raise the proxy's read timeout.
- **Nothing happens for half an hour after a request.** That is the delay profile giving your trackers a head
  start. A search you start yourself in Sonarr skips the delay.
- **The wrong series was suggested for a channel.** Search for the right one in Find and tap it. A wrong link can
  be removed on the Sources page.
- **Episode numbers look shifted.** Open the channel in Find and switch "Episode numbering" between Auto, As posted
  and Continuous. The choice is saved for that channel.
- **Telegram shows as disconnected.** tgarr retries by itself every 30 seconds. If it says you are not logged in,
  run the login command again.

## Development

```bash
pip install -r requirements.txt pytest
```

```bash
pytest -q
```

The app is FastAPI with Telethon and a single-file web page in `app/static`. `app/indexer.py` is the Newznab side,
`app/sab.py` the SABnzbd-compatible side, `app/catalog.py` the directories and the Wanted logic, and `app/parse.py`
the season and episode parsing.

`deploy.sh` is a shortcut for one particular server: it copies the source over SSH, builds the image there,
restarts the container and re-tests the indexer in Prowlarr and Sonarr. It is not needed when you run the published
image.

## License

[MIT](LICENSE). You are responsible for how you use it: download only what you have the right to, and follow
Telegram's terms for your account.
