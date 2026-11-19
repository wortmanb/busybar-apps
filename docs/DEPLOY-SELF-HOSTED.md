# Self-Hosted Deployment — infra.lab.thewortmans.org

This document explains how the `wortmanb/busybar-apps` fork is deployed to
`busybar-apps.lab.thewortmans.org` on `infra.lab.thewortmans.org` (192.168.10.197).

---

## Architecture overview

```
GitHub Actions
  build job  ──► ubuntu-latest runner  ──► builds ./dist, uploads artifact
  deploy-lab ──► self-hosted [infra]   ──► extracts artifact, swaps /srv/busybar-apps symlink

infra.lab (Caddy)
  busybar-apps.lab.thewortmans.org  ──► file_server /srv/busybar-apps
```

The build job runs on GitHub-hosted `ubuntu-latest`. Only the final publish
step touches the LAN host, which is not reachable from the internet.

---

## Repo variables (fork Settings → Variables → Actions)

| Variable    | Value for wortmanb fork                          | Purpose                     |
|-------------|--------------------------------------------------|-----------------------------|
| `SITE_URL`  | `https://busybar-apps.lab.thewortmans.org`       | Astro `site` config         |
| `BASE_PATH` | `/`                                              | Astro `base` config         |

These are injected at build time only for the `wortmanb/busybar-apps` repository.
The upstream repo (`maxswinkels/busybar-apps`) builds without these vars and
retains its original GitHub Pages configuration (`site: https://maxswinkels.github.io`,
`base: /busybar-apps/`).

---

## Self-hosted runner setup (one-time)

### 1. Create a dedicated low-privilege user

```bash
sudo useradd -r -m -d /opt/gh-runner -s /sbin/nologin gh-runner
sudo mkdir -p /srv/busybar-apps
sudo chown gh-runner:gh-runner /srv/busybar-apps
```

### 2. Install the Actions runner

Note: `/opt/gh-runner` is mode 750 (owned by `gh-runner`), so use
`sudo -u gh-runner` or `sudo bash -c` rather than a plain `cd`.

```bash
# Check https://github.com/actions/runner/releases for the latest version
RUNNER_VERSION=2.338.2
sudo -u gh-runner bash -c "
  mkdir -p /opt/gh-runner/actions-runner
  cd /opt/gh-runner/actions-runner
  curl -fsSL -o runner.tar.gz \
    https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz
  tar xzf runner.tar.gz
  rm runner.tar.gz
"
```

### 3. Register (you need a short-lived token)

Go to **https://github.com/wortmanb/busybar-apps/settings/actions/runners/new**
and copy the token shown (`AXXXXXXXXXX...`).

```bash
sudo -u gh-runner /opt/gh-runner/actions-runner/config.sh \
  --url https://github.com/wortmanb/busybar-apps \
  --token <PASTE_TOKEN_HERE> \
  --name infra-lab \
  --labels infra \
  --runnergroup Default \
  --unattended
```

### 4. Install as a systemd service

The `/opt/gh-runner` directory is mode 750 (owned by `gh-runner`), so a plain
`cd` will fail for other users. Use `sudo bash -c` to run the svc commands
from within the directory:

```bash
sudo bash -c "cd /opt/gh-runner/actions-runner && ./svc.sh install gh-runner"
sudo bash -c "cd /opt/gh-runner/actions-runner && ./svc.sh start"
sudo systemctl status actions.runner.wortmanb-busybar-apps.infra-lab.service
```

### 5. Verify

The runner should appear as **Online** at
`https://github.com/wortmanb/busybar-apps/settings/actions/runners`.

---

## Caddy configuration

`busybar-apps.lab.thewortmans.org` is served directly by Caddy from
`/srv/busybar-apps` using a read-only bind mount added to the caddy container
(`/srv/busybar-apps:/srv/busybar-apps:ro`) and a `file_server` block:

```caddyfile
busybar-apps.lab.thewortmans.org {
    import wortmancert
    root * /srv/busybar-apps
    file_server
    encode gzip
}
```

The TLS certificate is the existing wildcard cert at `/etc/wortman-certs/`.

After any change to `/data/caddy/compose.yml` (adding the bind mount), reload:

```bash
cd /data/caddy && docker compose up -d --force-recreate caddy
```

After Caddyfile-only changes (no compose changes needed after the initial mount),
the caddy container auto-reloads its config. You can also trigger a manual reload:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

---

## Deployment flow

1. A push to `main` (or manual `workflow_dispatch`) triggers the `Deploy` workflow.
2. The `build` job on `ubuntu-latest` runs `npm ci && npm run build` with
   `SITE_URL` and `BASE_PATH` injected from repo variables.
3. The built `./dist` directory is uploaded as a `github-pages` artifact.
4. The `deploy-lab` job on the self-hosted `[infra]` runner:
   - Downloads and extracts the artifact to a temp directory.
   - Moves the extracted files to a timestamped staging dir under `/srv/`.
   - Atomically swaps the `/srv/busybar-apps` symlink to point at the new dir.
   - Removes the previous release directory.

This ensures the site is never in a partially-deployed state.

---

## Security notes

- **Only** the `deploy-lab` job targets `self-hosted` runners. The `ci.yml`,
  `pr-comment.yml`, and `apps-index.yml` workflows continue to run on
  GitHub-hosted runners.
- The `deploy-lab` job is gated by `if: github.repository == 'wortmanb/busybar-apps'`
  so fork PRs from outside collaborators cannot trigger it.
- **Recommended**: in the fork's Settings → Actions → General, set
  "Fork pull request workflows from outside collaborators" to
  **"Require approval for all outside collaborators"**. This prevents
  untrusted PRs from even queueing a workflow run.

---

## Exposing via Cloudflare Tunnel / ZTA (future)

When you are ready to expose the site beyond the LAN:

1. Install `cloudflared` on `infra.lab.thewortmans.org`.
2. Create a tunnel:  
   ```bash
   cloudflared tunnel create busybar-apps
   ```
3. Add an ingress rule pointing `busybar-apps.thewortmans.org` →
   `https://busybar-apps.lab.thewortmans.org` (or directly to the Caddy port).
4. Update `SITE_URL` repo variable to the new public hostname and re-run the
   workflow so Astro rebuilds asset paths correctly.
5. Optionally add a Cloudflare Access policy to restrict to your team.
