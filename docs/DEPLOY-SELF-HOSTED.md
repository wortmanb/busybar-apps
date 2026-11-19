# Lab Deployment — infra.lab.thewortmans.org (Pull-Based)

This document explains how the `wortmanb/busybar-apps` fork is deployed to
`busybar-apps.lab.thewortmans.org` on `infra.lab.thewortmans.org` (192.168.10.197).

---

## Architecture overview

```
GitHub Actions (ubuntu-latest)
  build job      ──► npm run build (with SITE_URL/BASE_PATH vars)
  deploy-lab job ──► force-push dist/ as orphan commit → wortmanb/busybar-apps@deploy-lab

infra.lab (systemd timer, every 3 min)
  busybar-deploy.service ──► git fetch deploy-lab (public HTTPS, no credentials)
                          ──► if SHA changed: git archive → /srv/busybar-apps-releases/<SHA>/
                          ──► atomic symlink swap: /srv/busybar-apps → new release

infra.lab (Caddy)
  busybar-apps.lab.thewortmans.org ──► file_server /srv/busybar-apps (via /srv bind mount)
```

**Security model:** The infra host only makes outbound HTTPS pulls to a public
GitHub repo. No inbound connections, no self-hosted runner, no credentials
stored anywhere. Code execution on infra from GitHub is impossible — only static
files are fetched and published.

---

## Repo variables (fork Settings → Variables → Actions)

| Variable    | Default if unset                                 | Purpose                     |
|-------------|--------------------------------------------------|-----------------------------|
| `SITE_URL`  | `https://busybar-apps.lab.thewortmans.org`       | Astro `site` config         |
| `BASE_PATH` | `/`                                              | Astro `base` config         |

If neither variable is set, the fork's workflow uses hardcoded fallback
defaults, so no user action is required. Setting them overrides the defaults.

The upstream repo (`maxswinkels/busybar-apps`) builds without these vars and
retains its original GitHub Pages configuration.

---

## How the deploy works (workflow side)

A push to `main` (or manual `workflow_dispatch`) triggers the `Deploy` workflow:

1. **`build`** job on `ubuntu-latest`: runs `npm ci && npm run build` with
   `SITE_URL`/`BASE_PATH` injected from repo variables (or defaults). Uploads
   `./dist` as a `github-pages` artifact.

2. **`deploy-lab`** job (`github.repository == 'wortmanb/busybar-apps'`):
   downloads the artifact, extracts it, and force-pushes the contents as a
   single orphan commit to the `deploy-lab` branch. Requires `contents: write`
   permission; uses `GITHUB_TOKEN` (short-lived, scoped to the run).

3. **`deploy-pages`** job (`github.repository == 'maxswinkels/busybar-apps'`):
   upstream GitHub Pages deployment (unchanged from upstream).

The `deploy-lab` branch is public and contains **only** the built static site
files; no source code is included.

---

## How the deploy works (infra side)

### systemd units

Two units are installed on `infra.lab.thewortmans.org`:

**`busybar-deploy.service`** — a oneshot service that:
1. Initialises a bare git repo at `/opt/busybar-deploy/repo.git` (on first run).
2. Fetches the `deploy-lab` branch with `--depth=1` over public HTTPS.
3. Compares the fetched SHA with the currently deployed SHA.
4. If changed: archives the commit into `/srv/busybar-apps-releases/<SHA>/`,
   atomically swaps `/srv/busybar-apps` to the new release (single `mv -Tf`
   syscall), prunes old releases (keeps last 2).
5. Logs everything to the systemd journal (`journalctl -u busybar-deploy`).

**`busybar-deploy.timer`** — fires `OnUnitActiveSec=3min` (3-minute poll)
with up to 30 s random jitter. Enabled at boot.

### Manual trigger

```bash
# Deploy immediately (wait for result)
sudo systemctl start busybar-deploy.service

# Watch the logs
journalctl -u busybar-deploy -f
```

### Check deploy status

```bash
# See last run time and next scheduled run
systemctl list-timers busybar-deploy.timer

# See the currently deployed SHA
cat /srv/busybar-apps/.deploy-sha

# List release history
ls /srv/busybar-apps-releases/
```

---

## Caddy configuration

`busybar-apps.lab.thewortmans.org` is served by Caddy using:

- **Bind mount:** `/srv:/srv:ro` added to the Caddy container so symlink
  swaps to `/srv/busybar-apps-releases/<SHA>` are visible inside the container.
- **Caddyfile block:**

  ```caddyfile
  busybar-apps.lab.thewortmans.org {
      import wortmancert
      root * /srv/busybar-apps
      file_server
      encode gzip
  }
  ```

  TLS uses the existing wildcard cert at `/etc/wortman-certs/`.

After any change to `/data/caddy/compose.yml`:

```bash
cd /data/caddy && docker compose up -d --force-recreate caddy
```

After Caddyfile-only changes:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

---

## Rollback

Each deploy keeps up to 2 previous releases in `/srv/busybar-apps-releases/`.
To roll back to the previous release:

```bash
# List available releases (newest first)
ls -1dt /srv/busybar-apps-releases/*/

# Point the symlink at the previous release
sudo -u gh-runner ln -sfn /srv/busybar-apps-releases/<PREVIOUS_SHA> /srv/busybar-apps.next
sudo -u gh-runner mv -Tf /srv/busybar-apps.next /srv/busybar-apps

# Verify
cat /srv/busybar-apps/.deploy-sha
```

---

## First-time infra setup (future hosts)

If reinstalling from scratch on a new host:

```bash
# 1. Create unprivileged user
sudo useradd -r -m -d /opt/gh-runner -s /sbin/nologin gh-runner

# 2. Create directories
sudo mkdir -p /opt/busybar-deploy /srv/busybar-apps-releases
sudo chown gh-runner:gh-runner /opt/busybar-deploy /srv/busybar-apps-releases

# 3. Place the deploy script
sudo cp busybar-deploy.sh /opt/busybar-deploy/deploy.sh
sudo chown gh-runner:gh-runner /opt/busybar-deploy/deploy.sh
sudo chmod 750 /opt/busybar-deploy/deploy.sh

# 4. Install systemd units
sudo cp busybar-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now busybar-deploy.timer

# 5. Add Caddy site block and /srv:/srv:ro bind mount (see above)
```

---

## Security notes

- The `deploy-lab` branch is public — anyone can read it, but only
  `GITHUB_TOKEN` from a workflow run on `wortmanb/busybar-apps` can push to it.
- The infra timer runs as `gh-runner` (no login shell, home at `/opt/gh-runner`).
  It can only write to `/opt/busybar-deploy` and `/srv`.
- `ProtectSystem=strict` in the service unit prevents writes outside
  `ReadWritePaths`.
- **Recommended:** in the fork's Settings → Actions → General, set
  "Fork pull request workflows from outside collaborators" to
  **"Require approval for all outside collaborators"** to prevent untrusted
  PRs from triggering a build.

---

## Exposing via Cloudflare Tunnel / ZTA (future)

When you are ready to expose the site beyond the LAN:

1. Install `cloudflared` on `infra.lab.thewortmans.org`.
2. Create a tunnel:
   ```bash
   cloudflared tunnel create busybar-apps
   ```
3. Add an ingress rule pointing `busybar-apps.thewortmans.org` →
   `https://busybar-apps.lab.thewortmans.org`.
4. Update the `SITE_URL` repo variable to the new public hostname and re-run
   the workflow so Astro rebuilds asset paths correctly.
5. Optionally add a Cloudflare Access policy to restrict to your team.
