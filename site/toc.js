// Marks the section being read in the contents column.
(() => {
  const links = new Map([...document.querySelectorAll('nav.toc a')].map(a => [a.hash.slice(1), a]));
  if (!links.size || !('IntersectionObserver' in window)) return;
  const visible = new Set();
  const mark = () => {
    const first = [...links.keys()].find(id => visible.has(id));
    for (const [id, a] of links) a.classList.toggle('on', id === first);
  };
  const observer = new IntersectionObserver(entries => {
    for (const e of entries) (e.isIntersecting ? visible.add(e.target.id) : visible.delete(e.target.id));
    mark();
  }, {rootMargin: '-20% 0px -60% 0px'});
  for (const id of links.keys()) {
    const section = document.getElementById(id);
    if (section) observer.observe(section);
  }
})();
