/**
 * Layout evaluation only; does not change a Workflow or application dependencies.
 * Usage: node scripts/evaluate-compact-layout.cjs /path/to/elk.bundled.js /tmp/report [workflow.json]
 */
const fs = require('node:fs');
const path = require('node:path');
const dagre = require('dagre');
const ELK = require(path.resolve(process.argv[2]));
const output = path.resolve(process.argv[3]);
fs.mkdirSync(output, { recursive: true });

function chain(count, prefix = 'n') {
  return {
    nodes: Array.from({ length: count }, (_, i) => ({ id: prefix + i, width: 224, height: 140 })),
    edges: Array.from({ length: count - 1 }, (_, i) => ({ id: `${prefix}${i}->${prefix}${i + 1}`, source: prefix + i, target: prefix + (i + 1) })),
  };
}
function mixed() {
  const graph = chain(18);
  graph.nodes[4].height = 360; // expanded approval/output
  graph.nodes[11].height = 280;
  graph.edges = graph.edges.filter(e => !['n2->n3', 'n3->n4', 'n6->n7', 'n7->n8'].includes(e.id));
  for (const [source, target] of [['n1', 'n3'], ['n2', 'n4'], ['n3', 'n5'], ['n5', 'n7'], ['n6', 'n8'], ['n7', 'n9']]) {
    graph.edges.push({ id: `${source}->${target}`, source, target });
  }
  graph.edges.push({ id: 'pair:n15->n10', source: 'n15', target: 'n10', pairing: true });
  return graph;
}
function fromWorkflow(workflow) {
  const entries = Object.entries(workflow).filter(([id]) => !id.startsWith('__'));
  const nodes = entries.map(([id]) => ({ id, width: 224, height: 140 }));
  const ids = new Set(nodes.map(n => n.id));
  const edges = entries.flatMap(([id, value]) => (value.children || []).filter(target => ids.has(target)).map(target => ({ id: `${id}->${target}`, source: id, target })));
  for (const [id, value] of entries) {
    const end = value.node_config?.loop_end_node_id;
    if (value.node_type === 'LoopBeginNode' && ids.has(end)) edges.push({ id: `pair:${id}->${end}`, source: end, target: id, pairing: true });
  }
  return { nodes, edges };
}
function elkGraph(graph, strategy, aspect) {
  return { id: 'root', layoutOptions: {
    'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.edgeRouting': 'ORTHOGONAL',
    'elk.aspectRatio': String(aspect), 'elk.layered.wrapping.strategy': strategy,
    'elk.spacing.nodeNode': '60', 'elk.layered.spacing.nodeNodeBetweenLayers': '80',
  }, children: graph.nodes.map(n => ({ ...n, layoutOptions: { 'elk.portConstraints': 'FIXED_POS' },
    ports: [
      { id: n.id + ':in', x: 0, y: 50, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'WEST' } },
      { id: n.id + ':out', x: n.width, y: 50, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'EAST' } },
      { id: n.id + ':loopIn', x: n.width / 2, y: n.height, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'SOUTH' } },
      { id: n.id + ':loopOut', x: n.width / 2, y: n.height, width: 0, height: 0, layoutOptions: { 'elk.port.side': 'SOUTH' } },
    ],
  })), edges: graph.edges.map(e => ({ id: e.id,
    sources: [e.source + (e.pairing ? ':loopOut' : ':out')],
    targets: [e.target + (e.pairing ? ':loopIn' : ':in')],
    layoutOptions: { 'elk.layered.priority.direction': e.pairing ? '0' : '10' },
  })) };
}
function overlap(a, b) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}
function crosses(a, b, n) {
  const eps = .01;
  if (Math.abs(a.x - b.x) < eps) return a.x > n.x + eps && a.x < n.x + n.width - eps && Math.max(a.y, b.y) > n.y + eps && Math.min(a.y, b.y) < n.y + n.height - eps;
  if (Math.abs(a.y - b.y) < eps) return a.y > n.y + eps && a.y < n.y + n.height - eps && Math.max(a.x, b.x) > n.x + eps && Math.min(a.x, b.x) < n.x + n.width - eps;
  throw Error('Expected orthogonal edge segments');
}
function inspect(layout, graph) {
  const nodes = layout.children;
  const overlaps = nodes.flatMap((n, i) => nodes.slice(i + 1).filter(m => overlap(n, m)).map(m => [n.id, m.id]));
  const crossings = [];
  for (const edge of layout.edges) {
    const original = graph.edges.find(e => e.id === edge.id);
    for (const section of edge.sections || []) {
      const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint];
      for (let i = 1; i < points.length; i++) for (const node of nodes) {
        if (node.id !== original.source && node.id !== original.target && crosses(points[i - 1], points[i], node)) crossings.push([edge.id, node.id]);
      }
    }
  }
  return { overlaps, crossings, missingRoutes: layout.edges.filter(e => !e.sections?.length).map(e => e.id) };
}
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
function svg(layout) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${layout.width} ${layout.height}" width="100%">`
    + layout.edges.flatMap(e => (e.sections || []).map(s => `<polyline fill="none" stroke="#5268a8" stroke-width="3" points="${[s.startPoint, ...(s.bendPoints || []), s.endPoint].map(p => `${p.x},${p.y}`).join(' ')}"/>`)).join('')
    + layout.children.map(n => `<g><rect x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="8" fill="#f0f3fa" stroke="#6b7897"/><text x="${n.x + 12}" y="${n.y + 28}" font-family="sans-serif" font-size="18">${escape(n.id)}</text></g>`).join('') + '</svg>';
}
(async () => {
  const elk = new ELK();
  const disconnected = { nodes: [...chain(8, 'a').nodes, ...chain(8, 'b').nodes], edges: [...chain(8, 'a').edges, ...chain(8, 'b').edges] };
  const scenarios = { small: chain(3), long: chain(40), mixed: mixed(), disconnected, chain200: chain(200) };
  if (process.argv[4]) scenarios.actual = fromWorkflow(JSON.parse(fs.readFileSync(process.argv[4], 'utf8')));
  const rows = [];
  for (const [name, graph] of Object.entries(scenarios)) {
    const g = new dagre.graphlib.Graph(); g.setGraph({ rankdir: 'LR', nodesep: 60, ranksep: 80 }); g.setDefaultEdgeLabel(() => ({}));
    graph.nodes.forEach(n => g.setNode(n.id, { width: n.width, height: n.height })); graph.edges.filter(e => !e.pairing).forEach(e => g.setEdge(e.source, e.target));
    const dagreStart = performance.now(); dagre.layout(g);
    rows.push({ name, mode: 'dagre', ms: Math.round(performance.now() - dagreStart), width: g.graph().width, height: g.graph().height });
    for (const aspect of [1.6, .65]) for (const strategy of ['OFF', 'MULTI_EDGE']) {
      const start = performance.now(); const layout = await elk.layout(elkGraph(graph, strategy, aspect));
      const row = { name, mode: strategy, aspect, nodes: layout.children.length, edges: layout.edges.length, ms: Math.round(performance.now() - start), width: layout.width, height: layout.height, ...inspect(layout, graph) };
      rows.push(row); const key = `${name}-${strategy}-${aspect}`;
      fs.writeFileSync(path.join(output, key + '.json'), JSON.stringify(layout)); fs.writeFileSync(path.join(output, key + '.svg'), svg(layout));
    }
  }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(rows, null, 2));
  if (rows.some(row => row.overlaps?.length || row.crossings?.length || row.missingRoutes?.length)) process.exitCode = 1;
  console.log(JSON.stringify(rows.map(r => ({ ...r, overlaps: r.overlaps?.length, crossings: r.crossings?.length, missingRoutes: r.missingRoutes?.length })), null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
