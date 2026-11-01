import { createPlayer, mountPlayback } from './ablauf-playback.js';

const motion = window.matchMedia('(prefers-reduced-motion: reduce)');

function mountPreview(container, svg, timelines) {
  let visible = false;
  let paused = false;
  const caption = container.querySelector('.workflow-caption');
  const scenario = container.querySelector('.workflow-scenario');
  const toggle = container.querySelector('.workflow-preview-toggle');
  const canPlay = () => visible && !paused && !motion.matches && !document.hidden;
  const player = createPlayer(svg, timelines, {
    onChange(state) {
      const timeline = timelines[state.scenario];
      const step = timeline.steps[state.step];
      scenario.textContent = timeline.name;
      caption.textContent = step ? [step.actor, step.caption].filter(Boolean).join(': ') : 'Play the workflow step by step.';
      if (state.status === 'ended') {
        // Run after ablauf has completed its own state update.
        queueMicrotask(() => {
          if (!canPlay()) return;
          player.select((state.scenario + 1) % timelines.length);
          player.play();
        });
      }
    },
  });
  function sync() {
    toggle.hidden = motion.matches;
    if (canPlay()) {
      if (player.state.status === 'ended') player.select((player.state.scenario + 1) % timelines.length);
      player.play();
    } else player.pause();
  }
  toggle.addEventListener('click', () => {
    paused = !paused;
    toggle.textContent = paused ? 'Play preview' : 'Pause preview';
    toggle.setAttribute('aria-pressed', String(paused));
    sync();
  });
  const observer = new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    sync();
  });
  observer.observe(container);
  motion.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  sync();
  return () => {
    observer.disconnect();
    motion.removeEventListener('change', sync);
    document.removeEventListener('visibilitychange', sync);
    player.destroy();
  };
}

function mountEntry(container, svg, timelines, urls) {
  const { player, destroy } = mountPlayback(container, svg, timelines);
  const select = player.select.bind(player);
  const scenarioIndex = name => Math.max(0, timelines.findIndex(t => t.slug === name));
  // Resolve once: pushState changes document.baseURI after the first tab switch.
  const scenarioUrls = urls.map(url => new URL(url, document.baseURI));
  select(scenarioIndex(container.dataset.scenario));
  if (!motion.matches) player.play();

  // The package's tabs (including keyboard navigation) use player.select.
  // Keep those native controls and give each selected scenario its own URL.
  player.select = index => {
    select(index);
    const url = scenarioUrls[player.state.scenario];
    if (url.pathname !== location.pathname) history.pushState(null, '', url);
  };
  function navigate() {
    const name = location.pathname.split('/').filter(Boolean).pop();
    select(scenarioIndex(name));
    if (!motion.matches) player.play();
  }
  window.addEventListener('popstate', navigate);
  return () => {
    window.removeEventListener('popstate', navigate);
    destroy();
  };
}

const cleanups = [];
for (const container of document.querySelectorAll('.workflow-player')) {
  const { timelines, urls } = JSON.parse(container.querySelector('.workflow-data').textContent);
  const svg = container.querySelector('svg');
  cleanups.push(container.dataset.mode === 'preview'
    ? mountPreview(container, svg, timelines)
    : mountEntry(container, svg, timelines, urls));
}
window.addEventListener('pagehide', event => {
  if (!event.persisted) cleanups.forEach(cleanup => cleanup());
});
