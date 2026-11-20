// Colour from cover art: a screen takes its accent and the glow behind its header from the playlist's cover (the
// cover's most present lively colour; greyish covers give a quiet grey). Ported from static/colour.js.
const cache = new Map<string, Promise<number[] | null>>();

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min, s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s, l];
}

const hsl = (h: number, s: number, l: number, a = 1) =>
  `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}% / ${a})`;

function pick(img: HTMLImageElement): number[] | null {
  const c = document.createElement("canvas");
  c.width = c.height = 32;
  const x = c.getContext("2d", {willReadFrequently: true})!;
  x.drawImage(img, 0, 0, 32, 32);
  const px = x.getImageData(0, 0, 32, 32).data;
  const bins = Array.from({length: 24}, () => ({w: 0, r: 0, g: 0, b: 0}));
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

function load(url: string | null): Promise<number[] | null> {
  if (!url) return Promise.resolve(null);
  if (!cache.has(url)) {
    cache.set(url, new Promise((resolve) => {
      const img = new Image();
      img.onload = () => { try { resolve(pick(img)); } catch { resolve(null); } };
      img.onerror = () => resolve(null);
      img.src = url;
    }));
  }
  return cache.get(url)!;
}

// Colours already worked out, kept on the device too: a playlist opened again takes its colours at once instead of
// switching to them a moment later (the Find music button and stars flickered into colour, 2026-11-20).
const known = new Map<string, number[] | null>(Object.entries(stored()));

function stored(): Record<string, number[] | null> {
  try { return JSON.parse(localStorage.getItem("coverColours") || "{}"); } catch { return {}; }
}

function remember(url: string, rgb: number[] | null): void {
  known.set(url, rgb);
  try {
    const all = Object.fromEntries([...known].slice(-200));
    localStorage.setItem("coverColours", JSON.stringify(all));
  } catch { /* only a speed-up */ }
}

function apply(rgb: number[] | null, el: HTMLElement): void {
  const [h, s] = rgb ? rgbToHsl(rgb[0], rgb[1], rgb[2]) : [220, 0.12];
  const sat = rgb ? Math.max(0.42, Math.min(s, 0.75)) : 0.12;
  el.style.setProperty("--accent", hsl(h, sat, 0.68));
  el.style.setProperty("--accent-soft", hsl(h, sat, 0.68, 0.16));
  el.style.setProperty("--glow", rgb ? hsl(h, Math.min(sat, 0.6), 0.34, 0.85) : "transparent");
}

export async function tint(url: string | null, el: HTMLElement = document.documentElement): Promise<void> {
  if (!url) return apply(null, el);
  if (known.has(url)) return apply(known.get(url)!, el);
  const rgb = await load(url);
  remember(url, rgb);
  apply(rgb, el);
}

/** Work out a cover's colours ahead (Home does it for every playlist), so its page opens in them. */
export async function learn(url: string | null): Promise<void> {
  if (!url || known.has(url)) return;
  remember(url, await load(url));
}
