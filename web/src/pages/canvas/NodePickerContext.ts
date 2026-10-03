import { createContext, useContext } from 'react';
export type NodePickerPoint = { x: number; y: number };
export const NodePickerContext = createContext<(point?: NodePickerPoint) => void>(() => {});
export const useNodePicker = () => useContext(NodePickerContext);
