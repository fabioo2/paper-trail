// Renders the PWA icons in public/icons/ (run with `npm run icons` in the frontend container).
// Dependency-free: shapes are rasterised with 4x4 supersampling and encoded as PNG with zlib.
// The artwork matches public/favicon.svg: a white receipt with a zig-zag tear on a green tile.
import { writeFileSync } from "node:fs";
import { deflateSync } from "node:zlib";

const BRAND = [0x1f, 0x6b, 0x4e];
const PAPER = [0xff, 0xff, 0xff];
const SAMPLES = 4;

const ICONS = [
  { file: "icon-192.png", size: 192, tile: "rounded", scale: 1 },
  { file: "icon-512.png", size: 512, tile: "rounded", scale: 1 },
  // Maskable: full-bleed background, artwork inside the central safe zone.
  { file: "icon-maskable-512.png", size: 512, tile: "square", scale: 0.8 },
  // iOS rounds the corners itself.
  { file: "apple-touch-icon.png", size: 180, tile: "square", scale: 0.9 },
];

function insideRoundedSquare(x, y, radius) {
  const dx = Math.max(radius - x, 0, x - (1 - radius));
  const dy = Math.max(radius - y, 0, y - (1 - radius));
  return dx * dx + dy * dy <= radius * radius;
}

function insideReceipt(x, y) {
  if (x < 0.3 || x > 0.7 || y < 0.2) return false;
  const phase = ((x - 0.3) / 0.08) % 1;
  const tooth = 0.04 * (1 - Math.abs(2 * phase - 1));
  return y <= 0.76 + tooth;
}

const TEXT_LINES = [
  [0.37, 0.63, 0.31],
  [0.37, 0.63, 0.4],
  [0.37, 0.55, 0.49],
  [0.5, 0.63, 0.61],
];

function onTextLine(x, y) {
  return TEXT_LINES.some(
    ([left, right, top]) =>
      x >= left && x <= right && y >= top && y <= top + 0.035,
  );
}

/** Colour and coverage of one sample point in unit coordinates. */
function sample(x, y, tile, scale) {
  const onTile = tile === "square" || insideRoundedSquare(x, y, 0.22);
  if (!onTile) return null;
  const ax = 0.5 + (x - 0.5) / scale;
  const ay = 0.5 + (y - 0.5) / scale;
  return insideReceipt(ax, ay) && !onTextLine(ax, ay) ? PAPER : BRAND;
}

function render({ size, tile, scale }) {
  const pixels = Buffer.alloc(size * (size * 4 + 1));
  for (let py = 0; py < size; py++) {
    const row = py * (size * 4 + 1);
    pixels[row] = 0; // PNG filter type: none
    for (let px = 0; px < size; px++) {
      const sum = [0, 0, 0];
      let covered = 0;
      for (let sy = 0; sy < SAMPLES; sy++) {
        for (let sx = 0; sx < SAMPLES; sx++) {
          const colour = sample(
            (px + (sx + 0.5) / SAMPLES) / size,
            (py + (sy + 0.5) / SAMPLES) / size,
            tile,
            scale,
          );
          if (!colour) continue;
          covered++;
          colour.forEach((channel, i) => (sum[i] += channel));
        }
      }
      const offset = row + 1 + px * 4;
      for (let i = 0; i < 3; i++) {
        pixels[offset + i] = covered ? Math.round(sum[i] / covered) : 0;
      }
      pixels[offset + 3] = Math.round((covered / SAMPLES ** 2) * 255);
    }
  }
  return pixels;
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

function png(size, pixels) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 6, 0, 0, 0], 8); // 8-bit RGBA, no interlace
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(pixels, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

for (const icon of ICONS) {
  const target = new URL(`../public/icons/${icon.file}`, import.meta.url);
  writeFileSync(target, png(icon.size, render(icon)));
  console.log(`wrote public/icons/${icon.file} (${icon.size}x${icon.size})`);
}
