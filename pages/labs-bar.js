/*
 * The labs bar: one line at the top of every page of the boardfarmdevs EasyMesh labs'
 * sites, linking every project's site. The projects serve three goals, EasyMesh
 * optimizer development, the OpenSync adapter and EasyMesh on physical hardware, on
 * the way to one EasyMesh system on wmediumd with native agents and OpenSync pods
 * together; the RF medium, the clients and the resources are shared by the labs.
 *
 * Shared: the same file (pages/labs-bar.js) in every repository with a site, the
 * umbrella easymesh-labs (whose site the home link opens) and each project listed
 * below; change it in all of them. pages/finish-site.py adds it to every built page:
 *   <script src="labs-bar.js" data-project="emosa-lab" defer></script>
 * A full-screen tool opts out with <meta name="labs-bar" content="off">.
 */
(() => {
  const OWNER = 'boardfarmdevs';
  const HOME = 'easymesh-labs';
  const GOAL =
    'One EasyMesh system on wmediumd: native EasyMesh agents and OpenSync pods, through the adapter, together';
  const GROUPS = [
    {
      name: 'EasyMesh optimizer',
      short: 'Optimizer',
      projects: [
        {
          repo: 'meta-cmf-bananapi-vcpe',
          name: 'RDK EasyMesh',
          about: 'RDK-B on Banana Pi: EasyMesh, virtual RF and medium, optimizer, interactive room',
        },
        {
          repo: 'prplmesh-lab',
          name: 'prplMesh',
          about: 'The same EasyMesh lab on native prplMesh',
        },
        {
          repo: 'easymesh-optimizer',
          name: 'Optimizer',
          about: 'The optimizer both labs run and its room service: how it observes, decides, steers and verifies',
        },
        {
          repo: 'easymesh-room-builder',
          name: 'Room builder',
          about: "Design the labs' rooms in the browser: floor plans, walls, agents, clients, movement",
        },
      ],
    },
    {
      name: 'OpenSync adapter',
      short: 'Adapter',
      projects: [
        {
          repo: 'emosa-lab',
          name: 'EMOSA',
          about: 'The EasyMesh-to-OpenSync adapter: existing OpenSync pods as EasyMesh agents',
        },
        {
          repo: 'opensync-lab',
          name: 'OpenSync',
          about: 'A representative router and OpenSync pods with virtual radios and clients',
        },
      ],
    },
    {
      name: 'Physical EasyMesh',
      short: 'Physical',
      projects: [
        {
          repo: 'easymesh-lab',
          name: 'Protocol lab',
          about: 'A from-scratch EasyMesh controller and teaching panel with certified extenders and real clients',
        },
      ],
    },
    {
      name: 'Shared by the labs',
      short: 'Shared',
      projects: [
        {
          repo: 'easymesh-medium',
          name: 'RF medium',
          about: 'Virtual radios, wmediumd and the rooms: the radio medium both optimizer labs run on',
        },
        {
          repo: 'easymesh-clients',
          name: 'Clients',
          about: "The labs' Wi-Fi clients: how they are built, configured and managed, and how they can do more",
        },
        {
          repo: 'easymesh-resources',
          name: 'Resources',
          about: "Shared material for the labs: the MV3 EasyMesh footprint and the production plan",
        },
      ],
    },
  ];
  // The sites are on the labs' own domains: the umbrella at mesh.vcpe.dev, every other
  // project at vcpe.dev/<repository>/ (OWNER.github.io redirects to them).
  const site = (repo) => (repo === HOME ? 'https://mesh.vcpe.dev/' : `https://vcpe.dev/${repo}/`);
  const script = document.currentScript;
  const current = script ? script.dataset.project : '';
  if (document.getElementById('labs-bar')) return;

  const style = document.createElement('style');
  style.textContent = `
#labs-bar {
  --lb-bg: #f3f4f6; --lb-fg: #374151; --lb-muted: #6b7280; --lb-line: #e5e7eb;
  --lb-accent: #1d4ed8; --lb-current-bg: #ffffff;
  all: initial; display: block; box-sizing: border-box; width: 100%;
  background: var(--lb-bg); border-bottom: 1px solid var(--lb-line);
  font: 500 13px/1.2 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  color: var(--lb-fg); position: relative; z-index: 2147483000;
}
@media (prefers-color-scheme: dark) {
  html:not([data-theme="light"]) #labs-bar {
    --lb-bg: #111318; --lb-fg: #d1d5db; --lb-muted: #9ca3af; --lb-line: #262a33;
    --lb-accent: #93b4ff; --lb-current-bg: #1c2029;
  }
}
html[data-theme="dark"] #labs-bar, #labs-bar.lb-dark {
  --lb-bg: #111318; --lb-fg: #d1d5db; --lb-muted: #9ca3af; --lb-line: #262a33;
  --lb-accent: #93b4ff; --lb-current-bg: #1c2029;
}
#labs-bar.lb-light {
  --lb-bg: #f3f4f6; --lb-fg: #374151; --lb-muted: #6b7280; --lb-line: #e5e7eb;
  --lb-accent: #1d4ed8; --lb-current-bg: #ffffff;
}
#labs-bar * { all: unset; box-sizing: border-box; }
#labs-bar .lb-row {
  display: flex; align-items: center; gap: 2px; margin: 0 auto;
  padding: 0 16px; min-height: 36px; overflow-x: auto; scrollbar-width: none;
}
#labs-bar .lb-row::-webkit-scrollbar { display: none; }
#labs-bar a {
  display: inline-block; color: inherit; text-decoration: none; white-space: nowrap;
  border-radius: 6px; padding: 6px 7px; cursor: pointer;
}
#labs-bar a:hover { color: var(--lb-accent); }
#labs-bar a:focus-visible { outline: 2px solid var(--lb-accent); outline-offset: 1px; }
#labs-bar a[aria-current="page"] {
  background: var(--lb-current-bg); color: var(--lb-accent);
  box-shadow: inset 0 0 0 1px var(--lb-line);
}
#labs-bar .lb-home { color: var(--lb-muted); padding-left: 0; }
#labs-bar .lb-group {
  display: flex; align-items: center; gap: 2px; margin-left: 10px;
  padding-left: 10px; border-left: 1px solid var(--lb-line);
}
#labs-bar .lb-label {
  color: var(--lb-muted); font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
  text-transform: uppercase; white-space: nowrap; margin-right: 4px;
}
#labs-bar .lb-source { margin-left: auto; color: var(--lb-muted); }
#labs-bar .lb-short { display: none; }
#labs-bar .lb-full { display: inline; }
@media (max-width: 1760px) {
  #labs-bar .lb-full { display: none; }
  #labs-bar .lb-short { display: inline; }
}
@media (max-width: 1366px) {
  #labs-bar a { padding-left: 5px; padding-right: 5px; }
  #labs-bar .lb-group { margin-left: 7px; padding-left: 7px; }
}
@media (max-width: 1340px) {
  #labs-bar .lb-row, #labs-bar .lb-group { gap: 1px; }
  #labs-bar a { padding-left: 4px; padding-right: 4px; }
  #labs-bar .lb-group { margin-left: 5px; padding-left: 5px; }
  #labs-bar .lb-label { margin-right: 2px; }
}
@media (max-width: 640px) {
  #labs-bar .lb-row { padding: 0 12px; }
  #labs-bar .lb-group { margin-left: 8px; padding-left: 8px; }
  #labs-bar .lb-full { display: none; }
  #labs-bar .lb-short { display: inline; }
}
`;

  const link = (href, text, className, title) => {
    const a = document.createElement('a');
    a.href = href;
    a.textContent = text;
    if (className) a.className = className;
    if (title) a.title = title;
    return a;
  };
  const nav = document.createElement('nav');
  nav.id = 'labs-bar';
  nav.setAttribute('aria-label', 'Lab projects');
  const row = document.createElement('div');
  row.className = 'lb-row';
  const home = link(site(HOME), 'EasyMesh labs', 'lb-home', GOAL);
  if (current === HOME) home.setAttribute('aria-current', 'page');
  row.append(home);
  let known = current === HOME;
  for (const group of GROUPS) {
    const box = document.createElement('div');
    box.className = 'lb-group';
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', group.name);
    const label = document.createElement('span');
    label.className = 'lb-label';
    const full = document.createElement('span');
    full.className = 'lb-full';
    full.textContent = group.name;
    const short = document.createElement('span');
    short.className = 'lb-short';
    short.textContent = group.short;
    label.title = group.name;
    label.append(full, short);
    box.append(label);
    for (const project of group.projects) {
      const a = link(site(project.repo), project.name, '', project.about);
      if (project.repo === current) {
        a.setAttribute('aria-current', 'page');
        known = true;
      }
      box.append(a);
    }
    row.append(box);
  }
  if (known) row.append(link(`https://github.com/${OWNER}/${current}`, 'Source', 'lb-source'));
  nav.append(row);
  document.head.append(style);
  document.body.prepend(nav);

  // Match the page, not only the system theme: a site that is always dark gets
  // the dark bar. Uses the first opaque background colour of body, then html;
  // with only a background image, light body text means a dark page.
  const luma = (css) => {
    const rgba = (css || '').match(/[\d.]+/g);
    if (!rgba || rgba.length < 3 || (rgba.length === 4 && Number(rgba[3]) === 0)) return null;
    const [r, g, b] = rgba.map(Number);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const match = () => {
    nav.classList.remove('lb-dark', 'lb-light');
    for (const element of [document.body, document.documentElement]) {
      const level = luma(getComputedStyle(element).backgroundColor);
      if (level !== null) {
        nav.classList.add(level < 128 ? 'lb-dark' : 'lb-light');
        return;
      }
    }
    const body = getComputedStyle(document.body);
    const text = luma(body.color);
    if (body.backgroundImage !== 'none' && text !== null) {
      nav.classList.add(text > 160 ? 'lb-dark' : 'lb-light');
    }
  };
  match();
  const scheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (scheme && scheme.addEventListener) scheme.addEventListener('change', match);
  new MutationObserver(match).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class', 'data-theme', 'style'],
  });
  // On a narrow screen the row scrolls: keep the current project in sight.
  const here = row.querySelector('[aria-current="page"]');
  if (here && row.scrollWidth > row.clientWidth) {
    row.scrollLeft = here.offsetLeft - (row.clientWidth - here.offsetWidth) / 2;
  }
})();
