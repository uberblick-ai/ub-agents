import { createPlayer, mountPlayback } from './ablauf-playback.js';

const motion = window.matchMedia('(prefers-reduced-motion: reduce)');

// A card plays its scenarios in turn while it is in view.
function mountPreview(container, svg, timelines) {
  let visible = false;
  const canPlay = () => visible && !motion.matches && !document.hidden;
  const player = createPlayer(svg, timelines, {
    onChange(state) {
      if (state.status !== 'ended') return;
      // Run after ablauf has completed its own state update.
      queueMicrotask(() => {
        if (!canPlay()) return;
        player.select((state.scenario + 1) % timelines.length);
        player.play();
      });
    },
  });
  function sync() {
    if (canPlay()) {
      if (player.state.status === 'ended') player.select((player.state.scenario + 1) % timelines.length);
      player.play();
    } else player.pause();
  }
  const observer = new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    sync();
  }, { threshold: 0.45 });
  observer.observe(container);
  motion.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  return () => {
    observer.disconnect();
    motion.removeEventListener('change', sync);
    document.removeEventListener('visibilitychange', sync);
    player.destroy();
  };
}

function copyOnClick(button, text, done) {
  button?.addEventListener('click', () => {
    navigator.clipboard?.writeText(text()).then(done, () => {});
  });
}

function mountEntry(container, svg, timelines, urls) {
  // autoplay pauses the figure while it is out of view and resumes it on return.
  const { player, destroy } = mountPlayback(container, svg, timelines, { autoplay: true });
  const select = player.select.bind(player);
  const scenarioIndex = name => Math.max(0, timelines.findIndex(t => t.slug === name));
  // Resolve once: pushState changes document.baseURI after the first tab switch.
  const scenarioUrls = urls.map(url => new URL(url, document.baseURI));
  select(scenarioIndex(container.dataset.scenario));
  // Selecting leaves the player paused, which autoplay respects as the reader's
  // choice; start it so autoplay's first look holds it until it is in view.
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

copyOnClick(document.querySelector('.workflow-copy'), () => document.querySelector('.workflow-copy').dataset.prompt, () => {
  document.querySelector('.workflow-copy').textContent = 'Copied';
});
const link = document.querySelector('.workflow-link');
copyOnClick(link, () => new URL(location.pathname, link.dataset.url).href, () => {
  link.classList.add('copied');
  setTimeout(() => link.classList.remove('copied'), 1200);
});

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
