// node site/diagrams/render.mjs PATH/TO/ablauf issue issue-review-loop pr approvals
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [ablauf, ...names] = process.argv.slice(2);
if (!ablauf || !names.length) {
  throw new Error('Usage: node site/diagrams/render.mjs PATH/TO/ablauf NAME...');
}
const { render } = await import(pathToFileURL(resolve(ablauf, 'spike/flow/index.ts')).href);
for (const name of names) {
  const source = new URL(`${name}.flow`, import.meta.url);
  const { svg, diagnostics } = render(readFileSync(source, 'utf8'), { idPrefix: name });
  if (diagnostics.length) {
    throw new Error(`${name}: ${JSON.stringify(diagnostics)}`);
  }
  writeFileSync(new URL(`${name}.svg`, import.meta.url), svg);
  console.log(name, 'clean');
}
