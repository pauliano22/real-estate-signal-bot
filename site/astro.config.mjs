import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

export default defineConfig({
  site: 'https://pauliano22.github.io',
  base: '/real-estate-signal-bot',
  integrations: [tailwind()],
});
