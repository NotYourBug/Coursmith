"use strict";

// Native disclosure remains available when JavaScript is disabled.
(() => {
  const menu = document.querySelector(".sidebar-menu");
  if (!menu) return;
  const mobile = window.matchMedia("(max-width: 900px)");
  const adapt = () => { menu.open = !mobile.matches; };
  adapt();
  mobile.addEventListener("change", adapt);
})();
