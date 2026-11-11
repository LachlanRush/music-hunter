// Colour from cover art (2026-11-11): a page takes its soft accent and the glow behind its header from the playlist's
// cover, and the player bar its tint from the playing song's cover, instead of one fixed orange.
// Tint.apply(url, el?) sets --accent, --accent-soft, --glow (and --tint-rgb) on `el` (default: the page root).
const Tint = (() => {
  const cache = new Map();

  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
    if (max === min) return [0, 0, l];
    const d = max - min, s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [h * 60, s, l];
  }
  const hsl = (h, s, l, a = 1) => `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}% / ${a})`;

  // The cover's most present lively colour: pixels are grouped by hue (weighted by how saturated and mid-bright
  // they are); greyish covers fall back to a quiet neutral tint.
  function pick(img) {
    const c = document.createElement("canvas");
    c.width = c.height = 32;
    const x = c.getContext("2d", {willReadFrequently: true});
    x.drawImage(img, 0, 0, 32, 32);
    const px = x.getImageData(0, 0, 32, 32).data, bins = new Array(24).fill(null).map(() => ({w: 0, r: 0, g: 0, b: 0}));
    let total = 0;
    for (let i = 0; i < px.length; i += 4) {
      const [h, s, l] = rgbToHsl(px[i], px[i + 1], px[i + 2]);
      const w = s * (1 - Math.abs(l - 0.5) * 1.6);
      if (w <= 0.05) continue;
      const bin = bins[Math.floor(h / 15) % 24];
      bin.w += w; bin.r += px[i] * w; bin.g += px[i + 1] * w; bin.b += px[i + 2] * w; total += w;
    }
    const best = bins.reduce((a, b) => (b.w > a.w ? b : a));
    if (total < 20 || best.w < 8) return null;
    return [best.r / best.w, best.g / best.w, best.b / best.w];
  }

  function load(url) {
    if (!url) return Promise.resolve(null);
    if (!cache.has(url)) {
      cache.set(url, new Promise((resolve) => {
        const img = new Image();
        img.onload = () => { try { resolve(pick(img)); } catch (e) { resolve(null); } };
        img.onerror = () => resolve(null);
        img.src = url;
      }));
    }
    return cache.get(url);
  }

  function set(el, rgb) {
    const [h, s] = rgb ? rgbToHsl(...rgb) : [220, 0.12];
    const sat = rgb ? Math.max(0.42, Math.min(s, 0.75)) : 0.12;
    el.style.setProperty("--accent", hsl(h, sat, 0.68));          // text, stars, the progress bar
    el.style.setProperty("--accent-soft", hsl(h, sat, 0.68, 0.16));
    el.style.setProperty("--glow", hsl(h, Math.min(sat, 0.6), 0.34, 0.85));   // behind the header
    el.style.setProperty("--tint", hsl(h, Math.min(sat, 0.5), 0.2));         // the player bar
  }

  return {
    async apply(url, el = document.documentElement) {
      const rgb = await load(url);
      set(el, rgb);
      return rgb;
    },
    reset(el = document.documentElement) { set(el, null); },
  };
})();
