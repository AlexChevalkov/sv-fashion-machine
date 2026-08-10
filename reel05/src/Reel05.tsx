import React from "react";
import { AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig } from "remotion";
import { MONTSERRAT } from "./fonts";
import { CHARS_PER_SECOND, Reel05Props, Slide, slideFrames } from "./slides";

// Вёрстка повторяет текстовые карточки из visual_production_bot.py
// (POST_BG_COLOR, POST_MARGIN_LEFT и т.д.), чтобы ролик читался как та же
// карточка, только вытянутая в вертикаль и ожившая.
const BG = "rgb(18, 35, 30)";
const FG = "rgb(255, 255, 255)";
const COUNTER = "rgb(128, 145, 140)"; // фон + 110, как в боте
const MARGIN_LEFT = 128;
const MARGIN_RIGHT = 96;

const BRAND = "SV FASHION MEDIA";
const BODY_SIZE = 54;
const TITLE_SIZE = 76;

/** Сколько знаков уже набрано к этому кадру. */
const typedCount = (frame: number, fps: number): number =>
  Math.floor((frame / fps) * CHARS_PER_SECOND);

const Caret: React.FC<{ size: number }> = ({ size }) => {
  const frame = useCurrentFrame();
  const on = Math.floor(frame / 8) % 2 === 0;
  return (
    <span
      style={{
        display: "inline-block",
        width: size * 0.5,
        height: size * 0.08,
        marginLeft: size * 0.08,
        background: on ? FG : "transparent",
      }}
    />
  );
};

/**
 * Печатающийся блок текста.
 *
 * Полный текст рендерится прозрачным и держит высоту блока, а видимая часть
 * лежит поверх. Без этого блок растёт по мере набора и, будучи отцентрованным
 * по вертикали, всё время подпрыгивает.
 */
const Typed: React.FC<{
  full: string;
  shown: string;
  caret: boolean;
  size: number;
  weight: 400 | 700;
  lineHeight: number;
  style?: React.CSSProperties;
}> = ({ full, shown, caret, size, weight, lineHeight, style }) => {
  const base: React.CSSProperties = {
    fontFamily: MONTSERRAT,
    fontWeight: weight,
    fontSize: size,
    lineHeight,
    whiteSpace: "pre-wrap",
  };
  return (
    <div style={{ position: "relative", ...style }}>
      <div style={{ ...base, color: "transparent" }}>{full}</div>
      <div style={{ ...base, color: FG, position: "absolute", inset: 0 }}>
        {shown}
        {caret ? <Caret size={size} /> : null}
      </div>
    </div>
  );
};

const SlideView: React.FC<{ slide: Slide; index: number; total: number }> = ({
  slide,
  index,
  total,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const title = slide.title ? slide.title.toUpperCase() : "";
  const typed = typedCount(frame, fps);

  // Заголовок печатается первым, текст — следом, как если бы их набирали подряд.
  const titleShown = title.slice(0, typed);
  const bodyShown = slide.body.slice(0, Math.max(0, typed - title.length));
  const titleDone = typed >= title.length;
  const bodyDone = typed >= title.length + slide.body.length;

  return (
    <AbsoluteFill style={{ backgroundColor: BG }}>
      <div style={{ position: "absolute", left: MARGIN_LEFT, top: 108 }}>
        <div
          style={{
            fontFamily: MONTSERRAT,
            fontWeight: 700,
            fontSize: 30,
            letterSpacing: 1.5,
            color: FG,
          }}
        >
          {BRAND}
        </div>
        <div style={{ width: 120, height: 2, background: FG, marginTop: 22 }} />
      </div>

      <AbsoluteFill
        style={{
          paddingLeft: MARGIN_LEFT,
          paddingRight: MARGIN_RIGHT,
          paddingTop: 260,
          paddingBottom: 220,
          justifyContent: "center",
        }}
      >
        {title ? (
          <Typed
            full={title}
            shown={titleShown}
            caret={!titleDone}
            size={TITLE_SIZE}
            weight={700}
            lineHeight={1.16}
            style={{ marginBottom: 48 }}
          />
        ) : null}

        <Typed
          full={slide.body}
          shown={bodyShown}
          caret={titleDone && !bodyDone}
          size={BODY_SIZE}
          weight={400}
          lineHeight={1.44}
        />
      </AbsoluteFill>

      <div
        style={{
          position: "absolute",
          left: MARGIN_LEFT,
          bottom: 96,
          fontFamily: MONTSERRAT,
          fontWeight: 400,
          fontSize: 28,
          letterSpacing: 1,
          color: COUNTER,
        }}
      >
        {String(index + 1).padStart(2, "0")}/{String(total).padStart(2, "0")}
      </div>
    </AbsoluteFill>
  );
};

export const Reel05: React.FC<Reel05Props> = ({ slides }) => {
  let from = 0;
  return (
    <AbsoluteFill style={{ backgroundColor: BG }}>
      {slides.map((slide, i) => {
        const duration = slideFrames(slide);
        const start = from;
        from += duration;
        return (
          <Sequence
            key={i}
            from={start}
            durationInFrames={duration}
            premountFor={30}
          >
            <SlideView slide={slide} index={i} total={slides.length} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
