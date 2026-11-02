// mise run diagrams installs the pinned packages before running this script.
import { readdirSync, readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render } from '@uberblick/ablauf';
import { buildSync } from 'esbuild';

const directory = fileURLToPath(new URL('.', import.meta.url));
const requested = process.argv.slice(2);
const names = requested.length ? requested : readdirSync(directory)
  .filter(name => name.endsWith('.flow')).map(name => name.slice(0, -5)).sort();
for (const name of names) {
  if (!/^[a-z0-9-]+$/.test(name)) throw new Error(`Invalid diagram name: ${name}`);
  const source = new URL(`${name}.flow`, import.meta.url);
  const { svg, timelines, diagnostics } = render(readFileSync(source, 'utf8'), { idPrefix: name });
  if (diagnostics.length) {
    throw new Error(`${name}: ${JSON.stringify(diagnostics)}`);
  }
  writeFileSync(new URL(`${name}.svg`, import.meta.url), svg);
  writeFileSync(new URL(`${name}.timelines.json`, import.meta.url), JSON.stringify(timelines, null, 2) + '\n');
  console.log(name, 'clean');
}

const assets = new URL('../static/assets/', import.meta.url);
mkdirSync(assets, { recursive: true });
buildSync({
  stdin: {
    contents: "export { mountPlayback, createPlayer } from '@uberblick/ablauf/playback';",
    resolveDir: directory,
  },
  bundle: true,
  format: 'esm',
  target: 'es2020',
  banner: { js: '// Generated from @uberblick/ablauf@0.3.0 by site/diagrams/render.mjs.\n/*\n'
    + readFileSync(new URL('node_modules/@uberblick/ablauf/LICENSE', import.meta.url), 'utf8') + '*/' },
  outfile: fileURLToPath(new URL('ablauf-playback.js', assets)),
});
