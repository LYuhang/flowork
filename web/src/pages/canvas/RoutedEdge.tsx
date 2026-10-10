import { BaseEdge, BezierEdge, SmoothStepEdge, type EdgeProps, type XYPosition } from '@xyflow/react';
import { routedPath } from './layout-routes';

/** ELK supplies the bends; React Flow still owns selection, markers and labels. */
export function RoutedEdge(props: EdgeProps) {
  const points = props.data?.layoutPoints as XYPosition[];
  const first = points[0], last = points[points.length - 1];
  // Handles can move independently of the outer box (e.g. opening a node panel).
  if (Math.abs(first.x - props.sourceX) > 1 || Math.abs(first.y - props.sourceY) > 1
    || Math.abs(last.x - props.targetX) > 1 || Math.abs(last.y - props.targetY) > 1) {
    return props.data?.__pairing__ ? <SmoothStepEdge {...props} /> : <BezierEdge {...props} />;
  }
  const route = routedPath(points);
  return <BaseEdge id={props.id} {...route} label={props.label} labelStyle={props.labelStyle}
    labelShowBg={props.labelShowBg} labelBgStyle={props.labelBgStyle}
    labelBgPadding={props.labelBgPadding} labelBgBorderRadius={props.labelBgBorderRadius}
    style={props.style} markerStart={props.markerStart} markerEnd={props.markerEnd}
    interactionWidth={props.interactionWidth} />;
}
