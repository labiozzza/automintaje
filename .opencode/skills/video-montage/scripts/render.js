// Рендер HTML-ролика в кадры и mp4.
//   node render.js scene.html stills 0.5,2,4.2        → stills/000.50.jpg …
//   node render.js scene.html video                   → out/video.mp4 (весь ролик)
//   node render.js scene.html video 10 14 seg.mp4     → out/seg.mp4 (кусок 10–14 с)
// Страница должна выставить window.TL = {fps, w, h, end} и window.renderAt(t) (может быть async),
// и (по желанию) window.ready — Promise, который резолвится, когда шрифты и картинки загружены.
// Нужны: Node 18+, ffmpeg в PATH, пакет playwright (npm i playwright && npx playwright install chromium)
// или playwright-core + свой Chrome через env CHROME_PATH.
const path = require('path'), fs = require('fs'), http = require('http');
const { spawn } = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch { ({ chromium } = require('playwright-core')); }

const [file, mode = 'stills', a = '', b = '', outName = 'video.mp4'] = process.argv.slice(2);
if (!file) { console.log('usage: node render.js scene.html stills 0.5,2 | video [from] [to] [out.mp4]'); process.exit(1); }
const root = path.dirname(path.resolve(file)), page_name = path.basename(file);

// Локальный http вместо file:// — иначе маски, шрифты и fetch() из соседних файлов могут молча не загрузиться.
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.mp4': 'video/mp4', '.woff2': 'font/woff2' };
const server = http.createServer((req, res) => {
  const p = path.join(root, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(root) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(p).toLowerCase()] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});

(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const url = `http://127.0.0.1:${server.address().port}/${encodeURIComponent(page_name)}?render=1`;
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ['--force-color-profile=srgb'] });
  let page = await browser.newPage();
  page.on('pageerror', e => console.error('PAGE ERROR', e.message));
  await page.goto(url);
  const TL = await page.evaluate(() => window.TL);
  const W = TL.w || 1080, H = TL.h || 1920, FPS = TL.fps || 30;
  await page.setViewportSize({ width: W, height: H });
  await page.evaluate(() => window.ready);
  const shot = opts => page.screenshot({ clip: { x: 0, y: 0, width: W, height: H }, ...opts });

  if (mode === 'stills') {
    const dir = path.join(root, 'stills'); fs.mkdirSync(dir, { recursive: true });
    for (const t of a.split(',').filter(Boolean).map(Number)) {
      await page.evaluate(t => window.renderAt(t), t);
      await shot({ path: path.join(dir, `${t.toFixed(2).padStart(6, '0')}.jpg`), type: 'jpeg', quality: 80 });
    }
    console.log('stills →', dir);
  } else {
    const f0 = Math.round((+a || 0) * FPS), f1 = Math.round((+b || TL.end) * FPS);
    const out = path.join(root, 'out', outName); fs.mkdirSync(path.dirname(out), { recursive: true });
    const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
      '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
    for (let f = f0; f < f1; f++) {
      await page.evaluate(t => window.renderAt(t), f / FPS);
      const buf = await shot({ type: 'jpeg', quality: 94 });
      if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
      if (f % 150 === 0) console.log('frame', f, '/', f1);
    }
    ff.stdin.end(); await new Promise(r => ff.on('close', r)); console.log('done', out);
  }
  await browser.close(); server.close();
})().catch(e => { console.error(e); process.exit(1); });
