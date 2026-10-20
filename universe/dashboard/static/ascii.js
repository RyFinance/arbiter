// Background: an image redrawn as colored ASCII. Rows bend on a slow,
// non-linear wave like an old console scroller, and glyphs flicker. Drop a
// picture at static/bg.local.jpg (git-ignored); without one it renders a warm
// procedural gradient. Pure decoration: no data, no network beyond that file.
(() => {
  const canvas = document.getElementById("ascii");
  if (!canvas) return;
  const ctx = canvas.getContext("2d", {alpha: false});
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const SETTINGS = {
    cell: 11,            // glyph height in CSS px; width is ~0.6 of it
    ramp: " .'`:-i!l1t7r?2Z5634908&#@",
    brightness: 0.55,    // dimmed so the dashboard stays on top
    flicker: 0.012,      // share of glyphs swapped each frame
    wave: 0.7,           // wave strength in cells
    fps: 20,
    colorSteps: 6,       // per channel; batches fillText calls by color
    background: "#070504",
  };
  // data-theme="light" on the canvas: faint taupe glyphs on cream paper.
  const LIGHT = canvas.dataset.theme === "light";
  if (LIGHT) Object.assign(SETTINGS, {background: "#ebe6de", brightness: 1, flicker: 0.008});
  const TINT = LIGHT ? [150, 132, 112] : [238, 214, 186];
  const TINT_MIX = LIGHT ? 0.55 : 0.4;

  let cols = 0, rows = 0, cellW = 0, cellH = 0, dpr = 1;
  let lum = new Float32Array(0), rgb = new Uint8ClampedArray(0);
  let source = null;

  // Sample the source (image or gradient) into a cols x rows grid.
  const sampler = document.createElement("canvas");
  const sctx = sampler.getContext("2d", {willReadFrequently: true});

  function paintFallback(w, h) {
    const g = sctx.createLinearGradient(0, 0, w, h);
    g.addColorStop(0, "#1a0d06");
    g.addColorStop(0.45, "#7a3a14");
    g.addColorStop(0.7, "#d98a3a");
    g.addColorStop(1, "#2a1408");
    sctx.fillStyle = g;
    sctx.fillRect(0, 0, w, h);
  }

  function sample() {
    if (!cols || !rows) return; // image arrived before the first resize; start() samples it
    sampler.width = cols;
    sampler.height = rows;
    if (source) {
      // Stretch the whole picture to the screen; nothing is cropped.
      sctx.drawImage(source, 0, 0, cols, rows);
    } else {
      paintFallback(cols, rows);
    }
    const data = sctx.getImageData(0, 0, cols, rows).data;
    lum = new Float32Array(cols * rows);
    rgb = new Uint8ClampedArray(cols * rows * 3);
    for (let i = 0; i < cols * rows; i++) {
      const r = data[i * 4], g = data[i * 4 + 1], b = data[i * 4 + 2];
      // Light paper backgrounds would otherwise become a wall of '@';
      // invert luminance so ink and shadow carry the dense glyphs.
      lum[i] = 1 - (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      rgb[i * 3] = r; rgb[i * 3 + 1] = g; rgb[i * 3 + 2] = b;
    }
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cellH = SETTINGS.cell;
    cellW = Math.round(SETTINGS.cell * 0.6 * 10) / 10;
    canvas.width = Math.floor(window.innerWidth * dpr);
    canvas.height = Math.floor(window.innerHeight * dpr);
    cols = Math.ceil(window.innerWidth / cellW) + 2;
    rows = Math.ceil(window.innerHeight / cellH) + 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = `500 ${cellH}px "JetBrains Mono", ui-monospace, Menlo, monospace`;
    ctx.textBaseline = "top";
    sample();
  }

  const ramp = SETTINGS.ramp;
  const steps = SETTINGS.colorSteps;
  const quant = (v) => Math.round((v / 255) * (steps - 1));

  function frame(now) {
    const t = now / 1000;
    ctx.fillStyle = SETTINGS.background;
    ctx.fillRect(0, 0, window.innerWidth, window.innerHeight);
    const buckets = new Map();

    for (let y = 0; y < rows; y++) {
      // Two detuned sines plus a slow drift: bends like a CRT scroller, never repeats tidily.
      const shift = SETTINGS.wave * (
        Math.sin(y * 0.21 + t * 0.45) * 0.9 +
        Math.sin(y * 0.047 - t * 0.16) * 1.6 +
        Math.sin((y + t * 2) * 0.013) * 0.8
      );
      const rowGlitch = Math.random() < 0.0008 ? (Math.random() - 0.5) * 6 : 0;
      for (let x = 0; x < cols; x++) {
        const sx = Math.min(cols - 1, Math.max(0, Math.round(x - shift - rowGlitch)));
        const i = y * cols + sx;
        let l = lum[i];
        let glyph;
        if (Math.random() < SETTINGS.flicker) {
          glyph = ramp[1 + Math.floor(Math.random() * (ramp.length - 1))];
          l = Math.min(1, l + 0.25);
        } else {
          glyph = ramp[Math.min(ramp.length - 1, Math.floor(l * ramp.length))];
        }
        if (glyph === " ") continue;
        // Lift dark ink toward warm cream so dense glyphs stay legible on black.
        const f = SETTINGS.brightness * (LIGHT ? 1 : 0.6 + 0.4 * l);
        const lift = (c, t) => (c * (1 - TINT_MIX) + t * TINT_MIX) * f;
        const key = `${quant(lift(rgb[i * 3], TINT[0]))},${quant(lift(rgb[i * 3 + 1], TINT[1]))},${quant(lift(rgb[i * 3 + 2], TINT[2]))}`;
        let list = buckets.get(key);
        if (!list) buckets.set(key, (list = []));
        list.push(glyph, x * cellW - cellW, y * cellH);
      }
    }

    buckets.forEach((list, key) => {
      const [r, g, b] = key.split(",").map((v) => Math.round((v / (steps - 1)) * 255));
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      for (let k = 0; k < list.length; k += 3) ctx.fillText(list[k], list[k + 1], list[k + 2]);
    });
  }

  let last = 0;
  function loop(now) {
    if (document.hidden) return;
    if (now - last >= 1000 / SETTINGS.fps) {
      last = now;
      frame(now);
    }
    requestAnimationFrame(loop);
  }

  function start() {
    resize();
    frame(performance.now());
    if (!reduceMotion) requestAnimationFrame(loop);
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resize(); frame(performance.now()); }, 150);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !reduceMotion) requestAnimationFrame(loop);
  });

  const image = new Image();
  image.onload = () => { source = image; sample(); };
  image.src = "/static/bg.local.jpg";
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(start);
})();
