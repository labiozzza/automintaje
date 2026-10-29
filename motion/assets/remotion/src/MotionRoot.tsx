import React from 'react';
import {AbsoluteFill, Composition} from 'remotion';
import {MotionSceneView, MotionPlanProps} from './Scene';

const MotionComposition: React.FC<MotionPlanProps> = (props) => (
  <AbsoluteFill style={{backgroundColor: 'transparent', overflow: 'hidden'}}>
    {props.scenes.map((scene) => ({
      ...scene,
      content: {...scene.content, profile: scene.content.profile || props.profile},
    }))
      .filter((scene) => scene.review_status === 'accepted' && scene.effect_type !== 'none')
      .map((scene) => <MotionSceneView key={scene.scene_id} scene={scene} />)}
  </AbsoluteFill>
);

export const MotionRoot: React.FC = () => (
  <Composition
    id="MotionOverlay"
    component={MotionComposition}
    width={1080}
    height={1920}
    fps={30}
    durationInFrames={30}
    defaultProps={{
      scenes: [],
      durationMs: 1000,
      fpsNum: 30,
      fpsDen: 1,
      profile: 'minimal',
      renderMode: 'full',
    } as MotionPlanProps}
    calculateMetadata={({props}: {props: MotionPlanProps}) => {
      const fps = Math.max(1, props.fpsNum / Math.max(1, props.fpsDen));
      const durationMs = props.renderMode === 'preview'
        ? Math.min(props.durationMs, props.previewDurationMs || 15000)
        : props.durationMs;
      return {
        width: props.outputWidth || 1080,
        height: props.outputHeight || 1920,
        fps,
        durationInFrames: Math.max(1, Math.ceil(durationMs * fps / 1000)),
      };
    }}
  />
);
