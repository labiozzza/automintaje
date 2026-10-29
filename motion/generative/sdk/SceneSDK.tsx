import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';

export type SceneSDKProps = {
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
};

export type GeneratedSceneProps = SceneSDKProps;

export const clamp01 = (value: number): number => Math.max(0, Math.min(1, value));

export const progress = (frame: number, from: number, to: number): number =>
  interpolate(frame, [from, Math.max(from + 1, to)], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic),
  });

export const StaggeredWords: React.FC<{
  words: string[];
  frame: number;
  framesPerWord?: number;
  color?: string;
  fontSize?: number;
}> = ({words, frame, framesPerWord = 5, color = '#FFFFFF', fontSize = 70}) => {
  const {width} = useVideoConfig();
  const scale = width / 1080;
  return (
    <div style={{display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 18 * scale, flexWrap: 'wrap'}}>
      {words.map((word, index) => {
        const amount = progress(frame, index * framesPerWord, index * framesPerWord + 12);
        return <span key={`${index}-${word}`} style={{opacity: amount, color, fontSize: fontSize * scale,
          fontWeight: 900, transform: `translateY(${(1 - amount) * 36 * scale}px)`,
          fontFamily: 'Arial, Helvetica, sans-serif', textShadow: '0 4px 22px rgba(0,0,0,.42)'}}>{word}</span>;
      })}
    </div>
  );
};

export const SceneCanvas: React.FC<{children: React.ReactNode}> = ({children}) => (
  <AbsoluteFill style={{overflow: 'hidden', background: 'transparent'}}>{children}</AbsoluteFill>
);

export const useSceneClock = () => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  return {frame, fps, durationInFrames, localSeconds: frame / fps};
};
