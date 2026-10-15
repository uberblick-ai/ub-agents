# Loop diagrams

The `.flow` sources use [ablauf](https://github.com/uberblick-ai/ablauf).
The committed SVGs were rendered with ablauf commit `eda0dc5` and Node `v26.11.1`.
Use Node 22.18 or newer, which imports TypeScript without flags:

```sh
node site/diagrams/render.mjs PATH/TO/ablauf issue issue-review-loop pr approvals
```

Check out that ablauf commit before rendering. The script fails if any diagram
has diagnostics. Commit both the sources and rendered SVGs; `site/build.py`
inlines the SVGs without running Node.

The site's Google Fonts link loads IBM Plex Sans for ablauf's text measurements.
Keep the `--ab-*` theme overrides on the `.flow` wrapper: setting them on the SVG
itself can create a variable cycle with ablauf's own tokens and hide the strokes.
