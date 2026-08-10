// Зашивает Montserrat из ../fonts в сборку как data-URI.
//
// Шрифт нельзя грузить по сети: при рендере видео Remotion замораживает
// таймеры в странице, и ожидание сетевой загрузки там не разрешается —
// рендер упирается в тайм-аут delayRender и падает.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const fontsDir = join(here, "..", "..", "fonts");

const faces = [
  ["Montserrat-Regular.ttf", "REGULAR"],
  ["Montserrat-Bold.ttf", "BOLD"],
];

const lines = [
  "// СГЕНЕРИРОВАНО scripts/make-font-data.mjs — не редактировать вручную.",
  "",
];

for (const [file, name] of faces) {
  const b64 = readFileSync(join(fontsDir, file)).toString("base64");
  lines.push(`export const ${name} = "data:font/ttf;base64,${b64}";`, "");
}

writeFileSync(join(here, "..", "src", "font-data.ts"), lines.join("\n"));
console.log("src/font-data.ts собран из", fontsDir);
