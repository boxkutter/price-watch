# Price Watch on Unraid

One compose file ([`docker-compose.yml`](docker-compose.yml)) and one env file
([`.env.example`](.env.example) → `.env`). It runs four containers: PostgreSQL, the API (with the
admin page), the worker that contacts stores, and a one-shot helper that prepares the data folder.
Nothing else is needed, and the service is private: you invite the people who may use it.

## Install

You need Unraid 6.12 or newer with Docker enabled. The repository is not needed on the server: the
image is pulled from Docker Hub (`desertlee/price-watch:latest`), and you only place two small
files next to each other.

**1. Put the two files on the server**: `docker-compose.yml` and `.env.example`, from this folder of
the repository, into a new folder (Unraid terminal, from your computer: `scp` them there):

```bash
mkdir -p /mnt/user/appdata/retail-monitor/stack && cd /mnt/user/appdata/retail-monitor/stack
# copy docker-compose.yml and .env.example into this folder
```

**2. Create the env file** and fill in the four required values:

```bash
cp .env.example .env
nano .env        # POSTGRES_PASSWORD, APP_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD, and PUBLIC_BASE_URL
```

Generate the secrets with `openssl rand -hex 24` (database password) and `openssl rand -base64 48`
(`APP_SECRET`). Set `PUBLIC_BASE_URL` to the address you will type in the browser, for example
`http://192.168.1.50:8085`. If something is missing or still a placeholder, the API stops with a
message naming the setting (`docker compose logs api`).

**3. Start it:**

```bash
docker compose up -d
```

The first start pulls the image, which takes a minute. When `docker compose ps` shows `api` and `postgres` as
`healthy`, open **`http://<server-ip>:8085/admin`** and sign in with `ADMIN_EMAIL` and
`ADMIN_PASSWORD`.

**Using the Docker Compose Manager plugin instead:** add a stack, set its folder to
`/mnt/user/appdata/retail-monitor/stack`, edit the env file there, and press **Compose
Up**. The containers then show in the Docker tab with a WebUI shortcut to the admin page.

## First steps

1. **Connectors** (admin page): City Gross, Willys, Lidl and the demo shop work out of the box. Coop
   needs its API key entered on its card (see its note). ICA is authorized and has a connector, which is off until you have tried it (see its note).
2. **Try it without touching any real store:** open **example client** (`/demo`) from the admin page.
   It uses the synthetic demo shop, whose prices move every minute.
3. **Stores:** Willys and Lidl are one national online store each and exist already. For City Gross
   and Coop, use *discover stores* on the connector card.
4. **Invite people** or create an API client for your own app: see the API guide at
   `http://<server-ip>:8085/api-guide`.

## Where things are

| What | Where |
|---|---|
| Database | `${DATA_DIR}/postgres` (default `/mnt/user/appdata/retail-monitor/postgres`) |
| Connectors installed from the admin page | `${DATA_DIR}/connectors` |
| The Android app for `/download` | `${DATA_DIR}/downloads` (`pricewatch.apk` and `latest.json`, uploaded by `mobile/scripts/publish.sh`) |
| Bundled connectors | inside the image |
| Configuration | `.env` next to the compose file |

The folders are created, and owned by Unraid's `nobody:users` (99:100), automatically. If you
have a cache pool, point `DATA_DIR` at it (`/mnt/cache/appdata/retail-monitor`): PostgreSQL is
happier off the array's user-share layer.

## Reaching it safely

The admin page uses a password over plain HTTP on your LAN by default. That is fine on a trusted
home network; for anything wider, put a reverse proxy with HTTPS in front (SWAG or Nginx Proxy
Manager), set `BIND_ADDRESS=127.0.0.1` if the proxy runs on the same server, and set `PUBLIC_BASE_URL`
to the `https://` address. The service never needs to be reachable from the internet to work: it only
makes outgoing requests to the stores you enable.

## Update

```bash
cd /mnt/user/appdata/retail-monitor/stack
docker compose pull && docker compose up -d
```

Database changes are applied automatically when the API starts. Read the notes of a new version
first: this is a development project and updates may change things.

## Back up and restore

Back up `${DATA_DIR}` (the Unraid *Appdata Backup* plugin does this), or take a database dump:

```bash
docker compose exec -T postgres pg_dump -U retail retail | gzip > retail-$(date +%F).sql.gz
```

Restore into a stopped, empty database:

```bash
docker compose up -d postgres
gunzip -c retail-2026-11-26.sql.gz | docker compose exec -T postgres psql -U retail retail
docker compose up -d
```

`APP_SECRET` encrypts connector settings (for example Coop's key) in the database: keep it with the
backup, because a database restored under a different secret cannot read them (enter them again).

## Troubleshooting

| Symptom | Check |
|---|---|
| `api` restarts and `docker compose logs api` says "Cannot start: fix these settings" | The named variable in `.env` is missing, too short or still a placeholder |
| `variable is not set` when running compose | `.env` must be in the same folder as `docker-compose.yml` |
| Admin page loads but sign-in fails | Use `ADMIN_EMAIL`/`ADMIN_PASSWORD` as they were when the *first* start created the account; changing them later does not change an existing admin |
| Database password rejected after editing `POSTGRES_PASSWORD` | PostgreSQL keeps the password it was first created with. Change it inside: `docker compose exec postgres psql -U retail -c "ALTER USER retail PASSWORD 'new'"`, using the same value in `.env` |
| Installing a connector from the admin page fails with a permission error | `docker compose run --rm init` re-applies ownership of `${DATA_DIR}/connectors` |
| Willys checks only happen early in the morning | By design: Willys asks automated visits to stay within 04:00-08:45 UTC, so *scheduled* checks wait for it. Searches, adding by link and "check now" are never delayed |
| A connector shows "paused" | The store refused the service (HTTP 401/403/redirect). It is never worked around: review the connector's note, then resume it from its card |

Stop everything with `docker compose down` (data stays). Remove the data only deliberately, by
deleting `${DATA_DIR}`.

## Publishing the Android app

Phones install and update the app from `http://<server>:8085/download` (or your HTTPS address). You publish a
new version from the development computer, with nothing to copy by hand:

1. In this folder's `.env`, set `APP_UPLOAD_TOKEN` to a long random value (`openssl rand -base64 32`) and
   update the stack (`docker compose pull && docker compose up -d`). Leave it empty to switch uploading off.
2. On the development computer put the same value, and the server's address, in `mobile/.publish.env`
   (git-ignored): `PW_SERVER=https://price-watch.example.org` and `APP_UPLOAD_TOKEN=...`.
3. Run `mobile/scripts/publish.sh --bump --notes "What changed"`. It builds and signs the release and uploads it.

The server refuses anything that is not an APK, a file over 150 MB, a wrong token, and a build older than the one
on offer. A reverse proxy in front of it must allow uploads of about 50 MB (Nginx Proxy Manager: add
`client_max_body_size 100m;` under the host's Advanced tab).
