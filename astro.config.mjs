// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Allow deployment targets to override site/base via environment variables.
// Defaults preserve the upstream GitHub Pages configuration so upstream
// behaviour is unchanged when these vars are not set.
const site = process.env.SITE_URL ?? 'https://maxswinkels.github.io';
const base = process.env.BASE_PATH ?? '/busybar-apps/';

// https://astro.build/config
export default defineConfig({
  site,
  base,
  integrations: [sitemap()],
});
