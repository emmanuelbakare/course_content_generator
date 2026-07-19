(() => {
  const tabList = document.querySelector('.settings-tabs[role="tablist"]');
  if (!tabList) return;

  const tabs = Array.from(tabList.querySelectorAll('[role="tab"]'));
  const selectTab = (tab, focus = false) => {
    tabs.forEach((candidate) => {
      const selected = candidate === tab;
      const panel = document.getElementById(candidate.getAttribute('aria-controls'));
      candidate.setAttribute('aria-selected', String(selected));
      candidate.classList.toggle('is-active', selected);
      candidate.tabIndex = selected ? 0 : -1;
      if (panel) panel.hidden = !selected;
    });
    if (focus) tab.focus();
  };

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let nextIndex = index;
      if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      if (event.key === 'Home') nextIndex = 0;
      if (event.key === 'End') nextIndex = tabs.length - 1;
      selectTab(tabs[nextIndex], true);
    });
  });
})();
