import { z } from "zod";

export const slideSchema = z.object({
  /** Заголовок. Есть только у первого экрана, печатается перед текстом. */
  title: z.string().optional(),
  /** Текст экрана. Пустая строка внутри означает абзац. */
  body: z.string(),
});

export const reel05Schema = z.object({
  slides: z.array(slideSchema).min(1),
});

export type Slide = z.infer<typeof slideSchema>;
export type Reel05Props = z.infer<typeof reel05Schema>;

export const FPS = 30;

/**
 * Скорость набора в знаках в секунду и пауза после того, как экран дописан.
 * Подобраны на живом материале: набор идёт быстрее чтения, поэтому текст не
 * тормозит, а пауза даёт дочитать последнюю строку перед склейкой.
 */
export const CHARS_PER_SECOND = 42;
export const HOLD_SECONDS = 1.5;
export const MIN_SLIDE_SECONDS = 2.6;

export const slideSeconds = (slide: Slide): number => {
  const chars = (slide.title?.length ?? 0) + slide.body.length;
  return Math.max(MIN_SLIDE_SECONDS, chars / CHARS_PER_SECOND + HOLD_SECONDS);
};

export const slideFrames = (slide: Slide): number =>
  Math.round(slideSeconds(slide) * FPS);

export const totalFrames = (slides: Slide[]): number =>
  slides.reduce((sum, s) => sum + slideFrames(s), 0);

/** Текст-заглушка, чтобы композиция открывалась в студии без входных данных. */
export const SAMPLE_SLIDES: Slide[] = [
  {
    title: "Заголовок поста",
    body: "Первый экран: заголовок печатается, следом за ним текст.",
  },
  { body: "Второй экран.\n\nПустая строка внутри — это абзац." },
];
