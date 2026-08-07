// Diagram rendering and the theme switch.
//
// Inside Claude's artifact runtime both of these are free: mermaid renders natively and
// the host stamps data-theme on the root. Served as a plain page from the repository
// neither is, so this file supplies both -- which is the whole reason the page is split
// into three files rather than kept as one self-contained blob.

import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";

const root = document.documentElement;
const STORAGE_KEY = "deflect-architecture-theme";

// Captured before mermaid replaces each block with an <svg>, because re-rendering after a
// theme change needs the original graph text and by then the DOM no longer has it.
const sources = new Map();
for (const block of document.querySelectorAll("pre.mermaid")) {
  sources.set(block, block.textContent);
}

function currentTheme() {
  const chosen = root.getAttribute("data-theme");
  if (chosen) return chosen;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// Mermaid's own palettes clash with the page's brass accent, so the parts that carry
// meaning -- node fills, edge lines, label text -- are pinned to the same custom
// properties the rest of the stylesheet uses.
function mermaidConfig(theme) {
  const dark = theme === "dark";
  return {
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    themeVariables: {
      background: "transparent",
      primaryColor: dark ? "#161F2B" : "#F4F5F7",
      primaryTextColor: dark ? "#E4E8EF" : "#0E141B",
      primaryBorderColor: dark ? "#28313D" : "#D8DCE2",
      lineColor: dark ? "#7C8797" : "#6E7885",
      secondaryColor: dark ? "#1D1A12" : "#FAF3E4",
      tertiaryColor: dark ? "#111823" : "#FFFFFF",
      noteBkgColor: dark ? "#1D1A12" : "#FAF3E4",
      noteTextColor: dark ? "#E0B063" : "#9A6B15",
      actorBkg: dark ? "#161F2B" : "#F4F5F7",
      actorTextColor: dark ? "#E4E8EF" : "#0E141B",
      signalColor: dark ? "#A2ADBC" : "#495260",
      signalTextColor: dark ? "#A2ADBC" : "#495260",
    },
  };
}

async function renderDiagrams() {
  const theme = currentTheme();
  for (const [block, source] of sources) {
    block.removeAttribute("data-processed");
    block.textContent = source;
  }
  mermaid.initialize(mermaidConfig(theme));
  try {
    await mermaid.run({ nodes: [...sources.keys()] });
  } catch (error) {
    // A failed diagram must not take the document with it: the prose carries the
    // argument, the pictures only illustrate it.
    console.warn("mermaid failed to render", error);
  }
}

function applyTheme(theme) {
  root.setAttribute("data-theme", theme);
  localStorage.setItem(STORAGE_KEY, theme);
  const button = document.getElementById("theme-toggle");
  if (button) {
    button.setAttribute("aria-pressed", String(theme === "dark"));
    button.textContent = theme === "dark" ? "Light" : "Dark";
  }
  renderDiagrams();
}

const saved = localStorage.getItem(STORAGE_KEY);
if (saved === "dark" || saved === "light") {
  root.setAttribute("data-theme", saved);
}

document.getElementById("theme-toggle")?.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});

// Only when the reader has expressed no preference of their own -- otherwise their
// choice would be overridden every time the OS changed.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!localStorage.getItem(STORAGE_KEY)) renderDiagrams();
});

applyTheme(currentTheme());
