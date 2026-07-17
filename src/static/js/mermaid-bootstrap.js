/*
 * Local Mermaid integration. Only server-created .mermaid-diagram elements
 * are read, and their source arrives through an escaped data attribute.
 * The pinned Mermaid bundle is configured in strict mode. Fallback source
 * remains visible if Mermaid is missing or rendering fails.
 */
(() => {
  const mermaid = window.mermaid;
  if (!mermaid) return;

  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
  document.querySelectorAll('.mermaid-diagram[data-mermaid-source]').forEach((diagram, index) => {
    const source = diagram.dataset.mermaidSource;
    const fallback = diagram.querySelector('.mermaid-fallback');
    if (!source || !fallback) return;
    mermaid.render(`course-mermaid-${index}`, source)
      .then(({ svg }) => {
        const output = document.createElement('div');
        output.className = 'mermaid-rendered';
        // Mermaid is a pinned local dependency configured with securityLevel=strict.
        output.innerHTML = svg;
        diagram.append(output);
        fallback.hidden = true;
      })
      .catch(() => {
        diagram.dataset.mermaidStatus = 'failed';
      });
  });
})();
