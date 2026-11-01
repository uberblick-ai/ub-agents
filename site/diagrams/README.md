# Site diagrams and playback

The `.flow` sources use [ablauf](https://github.com/uberblick-ai/ablauf).
Workflow scenarios live in their source's `play` sections; captions, actors and
timing are rendered with the diagram, rather than maintained in site JavaScript.

With Node 22.18 or newer and npm installed, run from the repository root:

```sh
mise trust
mise run diagrams
```

The install task downloads `@uberblick/ablauf@0.3.0` and `esbuild@0.25.12` into
ignored `site/diagrams/node_modules/`. No ablauf checkout is needed. The render
script fails on diagnostics and writes each source's `.svg` and `.timelines.json`.
It also bundles the package's `./playback` entry once into
`site/static/assets/ablauf-playback.js`. Commit those outputs with the sources.
To render only selected diagrams after installation:

```sh
node site/diagrams/render.mjs workflow-default workflow-dependabot
```

`site/build.py` inlines the committed SVGs and timelines and copies static files;
it runs no Node and adds no Python or runtime npm dependency. The gallery script
only handles preview visibility and scenario cycling, scenario URLs and mounting
ablauf's controls. Adoption uses the existing `{{discuss NAME}}` buttons and
`site/prompts/` files. Workflow stories live under `site/content/workflows/`.

The site self-hosts IBM Plex Sans (`site/fonts`) for ablauf's text measurements.
Keep the `--ab-*` theme overrides on the `.flow` wrapper: setting them on the SVG
itself can create a variable cycle with ablauf's own tokens and hide the strokes.

When upgrading ablauf, re-render every source and inspect the existing SVG diff.
The pre-gallery SVGs were rendered with commit `eda0dc5`; any differences when
moving to 0.3.0 require maintainer acceptance under issue #782.
