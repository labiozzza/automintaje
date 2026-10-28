import React from 'react';
import {
  AbsoluteFill,
  Easing,
  interpolate,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

export type MotionScene = {
  scene_id: string;
  start_ms: number;
  end_ms: number;
  review_status: string;
  semantic_goal: string;
  effect_type: string;
  content: Record<string, unknown>;
  placement: {x: number; y: number; w: number; h: number; anchor: string};
  animation: {preset: string; enter_ms: number; exit_ms: number; delay_ms?: number};
};

export type MotionPlanProps = {
  scenes: MotionScene[];
  durationMs: number;
  fpsNum: number;
  fpsDen: number;
  outputWidth?: number;
  outputHeight?: number;
  profile: string;
  renderMode?: 'full' | 'preview';
  previewDurationMs?: number;
};

const palette: Record<string, {accent: string; panel: string; ink: string}> = {
  minimal: {accent: '#F4C95D', panel: 'rgba(13, 18, 28, 0.92)', ink: '#FFFFFF'},
  financial: {accent: '#5DD39E', panel: 'rgba(9, 27, 29, 0.94)', ink: '#FFFFFF'},
  'tech-ui': {accent: '#6DD8FF', panel: 'rgba(9, 20, 42, 0.94)', ink: '#F5FAFF'},
  editorial: {accent: '#FF8C69', panel: 'rgba(36, 20, 18, 0.94)', ink: '#FFF9F1'},
  custom: {accent: '#C5A6FF', panel: 'rgba(24, 20, 38, 0.94)', ink: '#FFFFFF'},
};

const asText = (value: unknown, fallback = ''): string =>
  value === undefined || value === null ? fallback : String(value);

const asNumber = (value: unknown, fallback = 0): number => {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
};

const useEntrance = (scene: MotionScene) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const durationFrames = Math.max(1, Math.round((scene.end_ms - scene.start_ms) * fps / 1000));
  const localFrame = frame - Math.round((scene.animation.delay_ms || 0) * fps / 1000);
  const enterFrames = Math.min(Math.max(1, Math.round(scene.animation.enter_ms * fps / 1000)), Math.max(1, Math.floor(durationFrames / 2)));
  const exitFrames = Math.min(Math.max(1, Math.round(scene.animation.exit_ms * fps / 1000)), Math.max(1, durationFrames - enterFrames));
  const enter = interpolate(localFrame, [0, enterFrames], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic),
  });
  const exitStart = Math.max(enterFrames, durationFrames - exitFrames);
  const exit = exitStart >= durationFrames ? 1 : interpolate(localFrame, [exitStart, durationFrames], [1, 0], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.in(Easing.cubic),
  });
  const preset = scene.animation.preset;
  const scale = preset === 'grow' || preset === 'grow-compare'
    ? interpolate(enter, [0, 1], [0.88, 1])
    : 1;
  const translateY = preset === 'slide-up'
    ? interpolate(enter, [0, 1], [44, 0])
    : preset === 'slide-down' ? interpolate(enter, [0, 1], [-44, 0]) : 0;
  return {opacity: Math.min(enter, exit), transform: `translateY(${translateY}px) scale(${scale})`};
};

const SceneFrame: React.FC<{scene: MotionScene; children: React.ReactNode}> = ({scene, children}) => {
  const style = useEntrance(scene);
  const box = scene.placement;
  const width = Math.max(0.05, Math.min(1, box.w));
  const height = Math.max(0.05, Math.min(1, box.h));
  const anchorTransform = box.anchor === 'center'
    ? 'translate(-50%, -50%)'
    : box.anchor === 'top-left' ? 'translate(0, 0)'
      : box.anchor === 'top-right' ? 'translate(-100%, 0)'
        : box.anchor === 'bottom-left' ? 'translate(0, -100%)'
          : 'translate(-100%, -100%)';
  return (
    <AbsoluteFill style={{
      left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${width * 100}%`,
      height: `${height * 100}%`, right: 'auto', bottom: 'auto',
      transform: `${anchorTransform} ${style.transform}`,
      opacity: style.opacity, alignItems: 'center', justifyContent: 'center',
    }}>
      {children}
    </AbsoluteFill>
  );
};

const Card: React.FC<{scene: MotionScene; title?: string; children: React.ReactNode}> = ({scene, title, children}) => {
  const {width} = useVideoConfig();
  const scale = width / 1080;
  const colors = palette[scene.content.profile as string] || palette.minimal;
  return (
    <div style={{
      width: '100%', height: '100%', boxSizing: 'border-box', padding: '3.5% 4%',
      borderRadius: 28, background: colors.panel, color: colors.ink,
      border: `2px solid ${colors.accent}`,
      fontFamily: 'Arial, Helvetica, sans-serif', display: 'flex', flexDirection: 'column',
      justifyContent: 'center', boxShadow: '0 10px 36px rgba(0,0,0,.22)',
    }}>
      {title && <div style={{fontSize: 42 * scale, fontWeight: 800, letterSpacing: 1.3 * scale, color: colors.accent, marginBottom: '2%'}}>{title}</div>}
      {children}
    </div>
  );
};

const TextEffect: React.FC<{scene: MotionScene}> = ({scene}) => {
  const {width} = useVideoConfig();
  const {fps} = useVideoConfig();
  const frame = useCurrentFrame();
  const scale = width / 1080;
  const content = scene.content;
  const title = asText(content.title || content.label || content.text);
  const subtitle = asText(content.subtitle || content.supporting_text);
  const colors = palette[asText(content.profile, 'minimal')] || palette.minimal;
  const reveal = interpolate(frame, [0, Math.max(1, Math.round(fps * .55))], [0, 100], {extrapolateRight:'clamp'});
  return (
    <SceneFrame scene={scene}>
      <div style={{width:'100%',height:'100%',clipPath:scene.effect_type === 'mask_reveal' ? `inset(0 ${100 - reveal}% 0 0)` : undefined}}>
      <Card scene={scene} title={title !== asText(content.text) ? title : ''}>
        {title === asText(content.text)
          ? <div style={{fontSize: 82 * scale, lineHeight: 1.08, fontWeight: 800, color: colors.ink}}>{title}</div>
          : null}
        {subtitle && <div style={{fontSize: 42 * scale, lineHeight: 1.2, marginTop: '2%'}}>{subtitle}</div>}
      </Card>
      </div>
    </SceneFrame>
  );
};

const StatCard: React.FC<{scene: MotionScene}> = ({scene}) => {
  const {width} = useVideoConfig();
  const {fps} = useVideoConfig();
  const frame = useCurrentFrame();
  const scale = width / 1080;
  const content = scene.content;
  const targetValue = asNumber(content.value);
  const counterProgress = interpolate(frame, [0, Math.max(1, Math.round(fps * .8))], [0, 1], {extrapolateRight:'clamp'});
  const value = scene.effect_type === 'counter'
    ? String(Math.round(asNumber(content.start_value) + (targetValue - asNumber(content.start_value)) * counterProgress))
    : asText(content.value);
  const unit = asText(content.unit);
  const caption = asText(content.caption);
  const colors = palette[asText(content.profile, 'minimal')] || palette.minimal;
  return (
    <SceneFrame scene={scene}>
      <Card scene={scene} title={asText(content.label || content.title)}>
        <div style={{fontSize: 190 * scale, lineHeight: 1, fontWeight: 900, color: colors.accent}}>{value}{unit ? <span style={{fontSize: '.48em', marginLeft: '.18em'}}>{unit}</span> : null}</div>
        {caption && <div style={{fontSize: 42 * scale, marginTop: '3%'}}>{caption}</div>}
      </Card>
    </SceneFrame>
  );
};

const ComparisonBars: React.FC<{scene: MotionScene}> = ({scene}) => {
  const {width} = useVideoConfig();
  const scale = width / 1080;
  const content = scene.content;
  const leftValue = Math.max(0, asNumber(content.left_value));
  const rightValue = Math.max(0, asNumber(content.right_value));
  const maxValue = Math.max(leftValue, rightValue, 1);
  const leftWidth = Math.max(3, leftValue / maxValue * 100);
  const rightWidth = Math.max(3, rightValue / maxValue * 100);
  const colors = palette[asText(content.profile, 'financial')] || palette.financial;
  return (
    <SceneFrame scene={scene}>
      <Card scene={scene} title={asText(content.title || scene.semantic_goal)}>
        {[['left_label', 'left_value', leftWidth], ['right_label', 'right_value', rightWidth]].map(([labelKey, valueKey, width], index) => (
          <div key={String(labelKey)} style={{marginTop: index ? '5%' : '0%'}}>
            <div style={{display: 'flex', justifyContent: 'space-between', fontSize: '5%', marginBottom: '1%'}}>
              <span style={{fontSize: 40 * scale}}>{asText(content[String(labelKey)])}</span>
              <strong style={{fontSize: 40 * scale}}>{asText(content[String(valueKey)])}{asText(content.unit) ? ` ${asText(content.unit)}` : ''}</strong>
            </div>
            <div style={{height: '9%', width: '100%', borderRadius: 999, background: 'rgba(255,255,255,.16)', overflow: 'hidden'}}>
              <div style={{height: '100%', width: `${width}%`, borderRadius: 999, background: index ? '#64B5F6' : colors.accent}} />
            </div>
          </div>
        ))}
      </Card>
    </SceneFrame>
  );
};

const ProcessFlow: React.FC<{scene: MotionScene}> = ({scene}) => {
  const {width} = useVideoConfig();
  const scale = width / 1080;
  const steps = Array.isArray(scene.content.steps) ? scene.content.steps.slice(0, 6) : [];
  const colors = palette[asText(scene.content.profile, 'tech-ui')] || palette['tech-ui'];
  return (
    <SceneFrame scene={scene}>
      <Card scene={scene} title={asText(scene.content.title || scene.semantic_goal)}>
        <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '2%', height: '60%'}}>
          {steps.map((step, index) => <React.Fragment key={index}>
            <div style={{flex: 1, minWidth: 0, borderRadius: 18, padding: '3%', background: 'rgba(255,255,255,.1)', textAlign: 'center', fontSize: 38 * scale, fontWeight: 700}}>{asText(step)}</div>
            {index < steps.length - 1 && <span style={{fontSize: 56 * scale, color: colors.accent}}>→</span>}
          </React.Fragment>)}
        </div>
      </Card>
    </SceneFrame>
  );
};

const ProgressArc: React.FC<{scene: MotionScene}> = ({scene}) => {
  const {width} = useVideoConfig();
  const scale = width / 1080;
  const value = Math.max(0, Math.min(100, asNumber(scene.content.value)));
  const colors = palette[asText(scene.content.profile, 'tech-ui')] || palette['tech-ui'];
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const local = Math.max(0, frame);
  const visible = interpolate(local, [0, Math.max(1, Math.round(fps * .7))], [0, value], {extrapolateRight: 'clamp'});
  const circumference = 2 * Math.PI * 86;
  return (
    <SceneFrame scene={scene}>
      <Card scene={scene} title={asText(scene.content.label || scene.content.title)}>
        <div style={{position: 'relative', width: '52%', aspectRatio: '1 / 1', margin: '0 auto'}}>
          <svg viewBox="0 0 200 200" width="100%" height="100%" style={{transform: 'rotate(-90deg)'}}>
            <circle cx="100" cy="100" r="86" stroke="rgba(255,255,255,.16)" strokeWidth="16" fill="none" />
            <circle cx="100" cy="100" r="86" stroke={colors.accent} strokeWidth="16" fill="none" strokeLinecap="round" strokeDasharray={circumference} strokeDashoffset={circumference * (1 - visible / 100)} />
          </svg>
          <div style={{position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', fontSize: 112 * scale, fontWeight: 900}}>{Math.round(visible)}%</div>
        </div>
      </Card>
    </SceneFrame>
  );
};

const HighlightBox: React.FC<{scene: MotionScene}> = ({scene}) => {
  const colors = palette[asText(scene.content.profile, 'minimal')] || palette.minimal;
  return (
    <SceneFrame scene={scene}>
      <AbsoluteFill style={{border: `4px solid ${colors.accent}`, borderRadius: 20, boxShadow: `0 0 30px ${colors.accent}80`}} />
    </SceneFrame>
  );
};

const ShapeEffect: React.FC<{scene: MotionScene}> = ({scene}) => {
  const colors = palette[asText(scene.content.profile, 'minimal')] || palette.minimal;
  const shape = asText(scene.content.shape, 'rounded-rect');
  if (shape === 'circle') {
    return <SceneFrame scene={scene}><div style={{width:'100%',height:'100%',borderRadius:'50%',border:`5px solid ${colors.accent}`,background:'rgba(255,255,255,.06)'}} /></SceneFrame>;
  }
  if (shape === 'line' || shape === 'underline') {
    return <SceneFrame scene={scene}><div style={{width:'100%',height:8,marginTop:'auto',background:colors.accent,borderRadius:999,boxShadow:`0 0 18px ${colors.accent}80`}} /></SceneFrame>;
  }
  if (shape === 'arrow') {
    return <SceneFrame scene={scene}><svg viewBox="0 0 300 120" width="100%" height="100%"><path d="M10 60 H255 M205 15 L255 60 L205 105" fill="none" stroke={colors.accent} strokeWidth="14" strokeLinecap="round" strokeLinejoin="round" /></svg></SceneFrame>;
  }
  if (shape === 'bracket') {
    return <SceneFrame scene={scene}><div style={{width:'100%',height:'100%',borderLeft:`8px solid ${colors.accent}`,borderTop:`8px solid ${colors.accent}`,borderBottom:`8px solid ${colors.accent}`,borderRadius:12}} /></SceneFrame>;
  }
  return <HighlightBox scene={scene} />;
};

export const MotionSceneView: React.FC<{scene: MotionScene}> = ({scene}) => {
  const {fps} = useVideoConfig();
  const Effect = (() => {
    switch (scene.effect_type) {
      case 'stat_card': case 'counter': return StatCard;
      case 'comparison_bars': case 'data_chart': return ComparisonBars;
      case 'process_flow': case 'timeline': return ProcessFlow;
      case 'progress_arc': return ProgressArc;
      case 'highlight_box': return HighlightBox;
      case 'mask_reveal': return TextEffect;
      case 'animated_text': case 'callout': return TextEffect;
      case 'shape': return ShapeEffect;
      default: return null;
    }
  })();
  if (!Effect) return null;
  const from = Math.max(0, Math.round(scene.start_ms * fps / 1000));
  const duration = Math.max(1, Math.round((scene.end_ms - scene.start_ms) * fps / 1000));
  return <Sequence from={from} durationInFrames={duration}><Effect scene={scene} /></Sequence>;
};
