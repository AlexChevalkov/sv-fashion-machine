
import { BOLD, REGULAR } from "./font-data";

export const MONTSERRAT = "Montserrat SV";

// Montserrat из репозитория sv-fashion-machine — тот же шрифт, которым бот
// рисует текстовые карточки, чтобы ролик совпадал с ними буква в букву.
//
// Файлы зашиты в сборку (font-data.ts), а не лежат в public/, потому что при
// рендере видео Remotion замораживает таймеры в странице: любое ожидание
// сетевой загрузки там ненадёжно и упирается в тайм-аут delayRender.
const FACES: Array<[string, number]> = [
  [REGULAR, 400],
  [BOLD, 700],
];

const style = document.createElement("style");
style.textContent = FACES.map(
  ([data, weight]) => `
@font-face {
  font-family: "${MONTSERRAT}";
  src: url("${data}") format("truetype");
  font-weight: ${weight};
  font-style: normal;
  font-display: block;
}`,
).join("\n");
document.head.appendChild(style);

// Ожидания загрузки здесь намеренно нет.
//
// При рендере видео Remotion замораживает таймеры в странице, и в этих
// условиях ни document.fonts.load(), ни setTimeout не разрешаются — рендер
// просто упирается в тайм-аут delayRender. Поскольку шрифт приходит из
// data-URI, сетевого запроса нет, а font-display: block заставляет браузер
// дождаться его перед первой отрисовкой текста. Проверено покадрово:
// первый кадр выходит уже в Montserrat.
