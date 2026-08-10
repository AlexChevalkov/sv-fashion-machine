import "./index.css";
import "./fonts";
import { CalculateMetadataFunction, Composition } from "remotion";
import { Reel05 } from "./Reel05";
import {
  FPS,
  Reel05Props,
  SAMPLE_SLIDES,
  reel05Schema,
  totalFrames,
} from "./slides";

// Длительность считается из самого текста: сколько знаков — столько и
// набирается. Поэтому её нельзя задать константой, она приходит из props.
const calculateMetadata: CalculateMetadataFunction<Reel05Props> = ({
  props,
}) => ({
  durationInFrames: totalFrames(props.slides),
});

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Reel05"
      component={Reel05}
      schema={reel05Schema}
      defaultProps={{ slides: SAMPLE_SLIDES }}
      calculateMetadata={calculateMetadata}
      durationInFrames={300}
      fps={FPS}
      width={1080}
      height={1920}
    />
  );
};
