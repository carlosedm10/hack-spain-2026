import type { Edge, EdgeProps } from "@xyflow/react";
import {
  BaseEdge,
  getBezierPath,
  getSmoothStepPath,
} from "@xyflow/react";

export type AnimatedSvgEdge = Edge<{
  duration?: number;
  path?: "bezier" | "smoothstep";
}>;

export function AnimatedSvgEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data = { duration: 3, path: "bezier" },
  style,
  ...rest
}: EdgeProps<AnimatedSvgEdge>) {
  const pathFn = data.path === "smoothstep" ? getSmoothStepPath : getBezierPath;
  const [path] = pathFn({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const duration = data.duration ?? 3;

  return (
    <>
      <BaseEdge id={id} path={path} style={style} {...rest} />
      <circle r="3" fill="currentColor">
        <animateMotion
          path={path}
          dur={`${duration}s`}
          repeatCount="indefinite"
          calcMode="linear"
          keyTimes="0;1"
          keyPoints="0;1"
        />
      </circle>
    </>
  );
}
