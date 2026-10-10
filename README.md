# tgarr

Telegram channels as a source for Sonarr and Radarr.

tgarr logs in to Telegram as your own account and does two jobs:

- **By hand:** a web page where you pick a channel, see its files with season and episode numbers worked out,
  and download them. Files are imported into Sonarr/Radarr with hardlinks and Plex is refreshed.
- **Automatically:** it presents itself to Sonarr as an indexer (Newznab, added through Prowlarr) and as a download
  client (SABnzbd-compatible). Request a show, and if your torrent trackers have nothing, Sonarr finds it on
  Telegram and tgarr downloads it.

Use it only for material you have the right to download.

## How the automatic path works

```
Overseerr -> Sonarr -> Prowlarr -> tgarr (indexer)  -> answers from an index of your linked channels
             Sonarr -> tgarr (download client)      -> downloads from Telegram into /data/telegram/tv
             Sonarr imports the finished file into the library -> Plex
```

- **Linked channels.** A channel answers Sonarr's searches once it is linked to a series. Linking happens by
  itself when the match is certain (an exact title match, or a series you picked yourself).
- **Numbering.** Channels often count seasons differently from TVDB. tgarr maps channel numbers onto Sonarr's
  episodes (posted as-is, or counted continuously when a channel season is longer than Sonarr's).
- **Directories.** A "directory" channel that lists one invite link per show gives tgarr English titles for
  channels named in another language, and powers the Wanted tab.
- **Hands-off mode.** When a requested show is still missing after the fallback delay and Sonarr has nothing
  queued for it, tgarr joins that show's directory channel, links it and asks Sonarr to search again. It only
  joins channels from your directories, with a daily limit. It can be switched off on the Sources page.

## Run it with Portainer

The image is built by GitHub Actions and published as `ghcr.io/omer182/tgarr:latest`.

1. **Create the config folder** on the host, owned by the user the container runs as:
   ```bash
   sudo mkdir -p /hosted-apps/tgarr/config && sudo chown 1000:1000 /hosted-apps/tgarr/config
   ```
2. **Add the stack.** Portainer -> Stacks -> Add stack -> Web editor, paste
   [docker-compose.prod.yml](docker-compose.prod.yml) and adjust the paths if yours differ. The `/data` mount
   must be the same one Sonarr and Radarr use, so that paths match and hardlinks work.
3. **Set the stack's environment variables** (see [.env.example](.env.example)): `TG_API_ID`, `TG_API_HASH`,
   `SONARR_KEY`, `RADARR_KEY` and optionally `PLEX_TOKEN`. Get the Telegram pair from
   <https://my.telegram.org> -> API development tools.
4. **Deploy the stack**, then log in to Telegram once (phone number, the code Telegram sends you, and your
   two-step password if you have one):
   ```bash
   docker exec -it tgarr python -m app.login
   docker restart tgarr
   ```
5. Open `http://<server>:8765`.

To update: Portainer -> the stack -> "Pull and redeploy". After a restart, run the indexer **Test** in Prowlarr
and Sonarr once, because they mark an indexer unavailable when it was briefly unreachable.

### Connect Sonarr and Prowlarr

The values are shown in the web UI under Sources -> Sonarr integration -> Show connection details.

- **Prowlarr:** Indexers -> Add -> Generic Newznab. URL `http://tgarr:8765/newznab`, API path `/api`, the API
  key from tgarr. Give it a low priority so trackers are preferred.
- **Sonarr:** Settings -> Download Clients -> Add -> SABnzbd. Host `tgarr`, port `8765`, URL base `sab`, the same
  API key, category `tv`.
- **Sonarr delay profile:** prefer Torrent and set a Usenet delay (for example 30 minutes) so trackers get a
  head start.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | | Telegram API credentials (required) |
| `SONARR_URL`, `SONARR_KEY` | `http://sonarr:8989` | Sonarr address and API key |
| `SONARR_ROOT`, `SONARR_PROFILE` | `/data/media/tv`, `6` | Root folder and quality profile for series tgarr adds |
| `RADARR_URL`, `RADARR_KEY` | `http://radarr:7878` | Radarr address and API key |
| `RADARR_ROOT`, `RADARR_PROFILE` | `/data/media/movies`, `6` | Root folder and quality profile for movies tgarr adds |
| `PLEX_URL`, `PLEX_TOKEN` | | Plex address and token (optional) |
| `PLEX_PATH_MAP` | `/data/media=/data/data/media` | Sonarr's path prefix = Plex's path prefix |
| `INDEX_REFRESH` | `600` | Seconds between channel index refreshes |
| `WORKERS` | `2` | Downloads running at once |

Everything tgarr remembers lives in `/config`: the Telegram session, sources, jobs, the index and the API key.
The session file gives full access to your Telegram account, so keep that folder private and out of any repo.

## Development

```bash
pip install -r requirements.txt pytest
pytest -q
```

`deploy.sh` is a shortcut for one particular server: it copies the source over SSH, builds the image there and
restarts the container. It is not needed when you run the published image.
