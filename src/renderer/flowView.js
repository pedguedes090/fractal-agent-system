// n8n-style SVG DAG — glassmorphism nodes, particle edges, phase groups, minimap.
// Vanilla JS — no framework required. Exposes window.FlowView.
(function () {
"use strict";

const NS = "http://www.w3.org/2000/svg";
const NODE_W = 176, NODE_H = 46, NODE_RX = 11, NODE_GAP = 18;
const LANE_W = 232, LANE_PAD_TOP = 58, LANE_TITLE_H = 36;
const PHASE_GAP = 18, PHASE_PAD = 12;

// Role → emoji icon (monospace-safe)
const ROLE_ICONS = {
  orchestrator: "⚙", intake: "📥", planner: "📋", critic: "🔍",
  governance: "🏛", coder: "</>", tester: "🧪", security_reviewer: "🛡",
  code_reviewer: "✓", release_deploy: "🚀", reporter: "📄",
  researcher_context: "📚", doctor: "🩺", reviewer: "⚖",
};

const PHASE_GROUPS = [
  { name: "INTAKE",     match: /^(preflight|codegraph_context|repo_intelligence|intake_)/ },
  { name: "PLANNING",   match: /^(planning_|plan_arbiter|critique_|planner_task_graph)/ },
  { name: "GOVERNANCE", match: /^(researcher_context_agent|governance_service|human_gate|environment_gate|read_only_reporter|load_context_files)/ },
  { name: "EXECUTION",  match: /^(openhands_worker|tester_agent)/ },
  { name: "REVIEW",     match: /^(security_reviewer_agent|code_reviewer_agent|doctor_feedback|release_deploy_agent|reviewer_decision|execution_gate)/ },
  { name: "FINALIZE",   match: /^(reporter|finalize_workspace|reporter_end)/ },
];
const PHASE_FILLS = [
  "#0b1220", "#0d1526", "#0b1220", "#0d1526", "#0b1220", "#0d1526",
];

const STATUS_COLORS = {
  idle:    { fill: "#1e293b", stroke: "#334155", text: "#94a3b8", glow: null },
  running: { fill: "#1e3a5f", stroke: "#3b82f6", text: "#bfdbfe", glow: "0 0 12px #3b82f688" },
  done:    { fill: "#064e3b", stroke: "#10b981", text: "#a7f3d0", glow: "0 0 8px #10b98144" },
  error:   { fill: "#450a0a", stroke: "#ef4444", text: "#fecaca", glow: "0 0 12px #ef444488" },
  skipped: { fill: "#1e293b", stroke: "#475569", text: "#64748b", glow: null },
};
const LANE_FILLS = ["#080d18", "#0a1020", "#080d18", "#0a1020", "#080d18", "#0a1020", "#080d18"];
const PARTICLE_COUNT = 4;

class FlowView {
  constructor(container, detailPanel) {
    this.container = container;
    this.detail = detailPanel;
    this.topology = null;
    this.nodeIndex = new Map();
    this.statusMap = new Map();
    this.lastEventFor = new Map();
    this.svg = null;
    this.selectedId = null;
    this._sticky = false;
    this.particles = [];
    this._particleTimer = null;
    this._boundHandler = (e) => this.onProgress(e.detail);
    window.addEventListener("agent:progress", this._boundHandler);
  }

  destroy() {
    window.removeEventListener("agent:progress", this._boundHandler);
    if (this._particleTimer) cancelAnimationFrame(this._particleTimer);
  }

  async load(fetchTopology) {
    try {
      this.topology = await fetchTopology();
      this._layout();
      this._render();
    } catch (e) {
      this.container.innerHTML =
        `<p class="muted" style="padding:24px">Không tải được topology: ${escapeHtml(e.message || String(e))}</p>`;
    }
  }

  setEventHistory(events) {
    this.statusMap.clear(); this.lastEventFor.clear();
    this._startedAt = null;
    if (this._elapsedTimer) { clearInterval(this._elapsedTimer); this._elapsedTimer = null; }
    for (const ev of events || []) this._applyProgress(ev, false);
    if (this.svg) this._refreshNodes();
    this._updateStatusBanner(null, null);
  }

  reset() {
    this.setEventHistory([]);
    this._sticky = false; this.selectedId = null;
    if (this.svg) this._refreshNodes();
    this._clearHighlight();
    if (this.detail) this.detail.innerHTML = `<p class="muted">Click một node để xem chi tiết.</p>`;
  }

  onProgress(progress) {
    if (!this.topology) return;
    this._applyProgress(progress, true);
  }

  _applyProgress(progress, redraw) {
    if (!progress) return;
    const stage = String(progress.stage || "");
    const node = String(progress.node || progress.stage || "");
    if (!this.nodeIndex.has(node)) return;
    this.lastEventFor.set(node, progress);
    let status = this.statusMap.get(node);
    if (stage === "error" || progress.error) status = "error";
    else if (/_(start|begin)$/.test(stage) || stage === "node_start") status = "running";
    else if (/_(end|complete|done)$/.test(stage) || stage === "node_end" || stage === "done") status = "done";
    else if (stage === "skipped") status = "skipped";
    else if (!status) status = "running";
    this.statusMap.set(node, status);
    if (redraw) {
      this._refreshNode(node);
      if (status === "running") this._pulseEdgesInto(node);
      if (this.selectedId === node) this._renderDetail(node);
    }
    this._updateStatusBanner(node, status);
  }

  _statusClass(status) {
    return { idle: "idle", running: "running", done: "done", error: "error", skipped: "idle" }[status] || "idle";
  }

  _updateStatusBanner(latestNode, latestStatus) {
    if (!this.statusEls) return;
    const totals = this.nodeIndex.size;
    let done = 0, running = 0, errored = 0;
    for (const s of this.statusMap.values()) {
      if (s === "done") done++;
      else if (s === "running") running++;
      else if (s === "error") errored++;
    }
    if (!this._startedAt && (running || done)) {
      this._startedAt = Date.now();
      if (this._elapsedTimer) clearInterval(this._elapsedTimer);
      this._elapsedTimer = setInterval(() => this._refreshElapsed(), 500);
    }
    const isRunning = running > 0 || (done > 0 && done < totals && !errored);
    const nodeInfo = this.nodeIndex.get(latestNode);
    const phaseLabel = nodeInfo ? (nodeInfo.phaseGroup || "") : "";
    this.statusEls.state.textContent = errored ? "LỖI" : running ? "ĐANG CHẠY" : done >= totals ? "HOÀN TẤT" : done ? "TẠM DỪNG" : "IDLE";
    this.statusEls.state.className = "flow-pill flow-pill-" + this._statusClass(errored ? "error" : running ? "running" : done >= totals ? "done" : "idle");
    this.statusEls.current.textContent = latestStatus === "running" && nodeInfo
      ? `→ ${phaseLabel ? phaseLabel + " · " : ""}${nodeInfo.label}`
      : (latestStatus === "error" && nodeInfo ? `× ${nodeInfo.label}` : (running ? "(đang xử lý)" : ""));
    this.statusEls.counts.textContent = `${done}/${totals}${errored ? ` · ${errored} lỗi` : ""}`;
    const pct = totals ? (done / totals) * 100 : 0;
    this.statusEls.fill.style.width = `${pct}%`;
    this.statusEls.fill.style.background = errored ? "#ef4444" : done >= totals ? "#10b981" : "linear-gradient(90deg, #3b82f6, #60a5fa)";
    if (!isRunning && this._elapsedTimer) { clearInterval(this._elapsedTimer); this._elapsedTimer = null; this._refreshElapsed(); }
  }

  _refreshElapsed() {
    if (!this.statusEls || !this._startedAt) return;
    const ms = Date.now() - this._startedAt;
    const s = Math.floor(ms / 1000);
    this.statusEls.elapsed.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  }

  _layout() {
    const mapping = {};
    for (const lane of this.topology.lanes) mapping[lane.id] = lane;
    const perLane = new Map();
    for (const lane of this.topology.lanes) perLane.set(lane.id, []);
    for (const node of this.topology.nodes) {
      const laneId = mapping[node.lane] ? node.lane : this.topology.lanes[0]?.id;
      const list = perLane.get(laneId) || [];
      list.push(node);
      perLane.set(laneId, list);
    }
    // Phase-group assignment
    for (const n of this.topology.nodes) {
      for (let p = 0; p < PHASE_GROUPS.length; p++) {
        if (PHASE_GROUPS[p].match.test(n.id)) { n._pi = p; n._pg = PHASE_GROUPS[p].name; break; }
      }
    }
    this.nodeIndex.clear();
    const laneArray = this.topology.lanes;
    for (let ci = 0; ci < laneArray.length; ci++) {
      const lane = laneArray[ci];
      const list = perLane.get(lane.id) || [];
      list.forEach((node, ri) => {
        const x = ci * LANE_W + (LANE_W - NODE_W) / 2;
        const y = LANE_PAD_TOP + ri * (NODE_H + NODE_GAP);
        this.nodeIndex.set(node.id, { ...node, x, y, lane: lane.id, ci, ri, phaseGroup: node._pg, phaseIndex: node._pi });
      });
    }
    const maxRows = Math.max(...Array.from(perLane.values()).map(l => l.length), 1);
    this.viewW = laneArray.length * LANE_W;
    this.viewH = LANE_PAD_TOP + maxRows * (NODE_H + NODE_GAP) + 40;
  }

  _render() {
    this.container.innerHTML = "";
    const layoutEl = this.container.closest(".flow-layout") || this.container;
    const existingBanner = layoutEl.parentElement?.querySelector(":scope > .flow-status-banner");
    if (existingBanner) existingBanner.remove();

    // Banner
    const banner = document.createElement("div");
    banner.className = "flow-status-banner";
    banner.innerHTML = `
      <div class="flow-status-row">
        <span class="flow-pill flow-pill-idle" data-role="state">IDLE</span>
        <span class="flow-current" data-role="current">Chưa chạy</span>
        <span class="flow-counts" data-role="counts">0/${this.nodeIndex.size}</span>
        <span class="flow-elapsed" data-role="elapsed"></span>
        <span class="flow-legend">
          <span class="legend-chip" style="background:#3b82f6"></span>fan-out
          <span class="legend-chip" style="background:#10b981"></span>join
          <span class="legend-chip legend-dash" style="background:#a78bfa"></span>route
          <span class="legend-chip" style="background:#475569"></span>flow
        </span>
      </div>
      <div class="flow-progressbar"><div class="flow-progressbar-fill" data-role="fill"></div></div>`;
    const gridParent = this.container.closest(".flow-layout") || this.container;
    gridParent.parentElement?.insertBefore(banner, gridParent);
    this.statusEls = {
      state: banner.querySelector('[data-role="state"]'),
      current: banner.querySelector('[data-role="current"]'),
      counts: banner.querySelector('[data-role="counts"]'),
      elapsed: banner.querySelector('[data-role="elapsed"]'),
      fill: banner.querySelector('[data-role="fill"]'),
    };

    // Toolbar
    const toolbar = document.createElement("div");
    toolbar.className = "flow-toolbar";
    toolbar.innerHTML = `
      <button title="Phóng to">+</button>
      <button title="Thu nhỏ">−</button>
      <button title="Vừa khung">⤢</button>`;
    this.container.appendChild(toolbar);
    toolbar.addEventListener("click", (e) => {
      const act = e.target?.textContent;
      if (act === "+") this._zoomBy(1.2, this._centerInClient());
      if (act === "−") this._zoomBy(1 / 1.2, this._centerInClient());
      if (act === "⤢") this._fit();
    });

    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "flow-svg");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.setAttribute("viewBox", `0 0 ${this.viewW} ${this.viewH}`);

    // SVG filters
    const defs = document.createElementNS(NS, "defs");
    defs.innerHTML = `
      <filter id="node-glow" x="-40%" y="-40%" width="180%" height="180%">
        <feGaussianBlur stdDeviation="4" result="blur"/>
        <feFlood flood-color="#3b82f6" flood-opacity="0.35"/>
        <feComposite in2="blur" operator="in"/>
        <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="node-glow-done" x="-40%" y="-40%" width="180%" height="180%">
        <feGaussianBlur stdDeviation="3" result="blur"/>
        <feFlood flood-color="#10b981" flood-opacity="0.25"/>
        <feComposite in2="blur" operator="in"/>
        <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="node-glow-error" x="-40%" y="-40%" width="180%" height="180%">
        <feGaussianBlur stdDeviation="5" result="blur"/>
        <feFlood flood-color="#ef4444" flood-opacity="0.4"/>
        <feComposite in2="blur" operator="in"/>
        <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="#334155"/>
      </marker>
      <marker id="arrow-active" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="#60a5fa"/>
      </marker>`;
    svg.appendChild(defs);

    // Viewport for pan/zoom
    const viewport = document.createElementNS(NS, "g");
    viewport.setAttribute("class", "flow-viewport");
    svg.appendChild(viewport);
    this.viewport = viewport;
    this.transform = { tx: 0, ty: 0, k: 1 };
    this._applyTransform();

    // Phase group backgrounds
    const phaseBounds = new Map();
    for (const [id, n] of this.nodeIndex.entries()) {
      if (n.phaseGroup == null) continue;
      const key = n.phaseGroup;
      let b = phaseBounds.get(key);
      if (!b) { b = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity, nodes: [] }; phaseBounds.set(key, b); }
      b.minX = Math.min(b.minX, n.x); b.minY = Math.min(b.minY, n.y);
      b.maxX = Math.max(b.maxX, n.x + NODE_W); b.maxY = Math.max(b.maxY, n.y + NODE_H);
      b.nodes.push(n);
    }
    const phaseLayer = document.createElementNS(NS, "g");
    let pi = 0;
    for (const [name, b] of phaseBounds.entries()) {
      const rx = b.minX - PHASE_PAD, ry = b.minY - PHASE_PAD + 6;
      const rw = b.maxX - b.minX + PHASE_PAD * 2, rh = b.maxY - b.minY + PHASE_PAD * 2 - 4;
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", rx); rect.setAttribute("y", ry);
      rect.setAttribute("width", rw); rect.setAttribute("height", rh);
      rect.setAttribute("rx", 12); rect.setAttribute("ry", 12);
      rect.setAttribute("fill", PHASE_FILLS[pi % PHASE_FILLS.length]);
      rect.setAttribute("stroke", "#1e293b");
      rect.setAttribute("stroke-width", "1");
      rect.setAttribute("stroke-dasharray", "6 4");
      rect.setAttribute("opacity", "0.65");
      phaseLayer.appendChild(rect);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", rx + 14); t.setAttribute("y", ry + 20);
      t.setAttribute("fill", "#475569"); t.setAttribute("font-size", "11");
      t.setAttribute("font-weight", "700"); t.setAttribute("letter-spacing", "1.5");
      t.setAttribute("font-family", "system-ui, sans-serif");
      t.textContent = name;
      phaseLayer.appendChild(t);
      pi++;
    }
    viewport.appendChild(phaseLayer);

    // Lane backgrounds
    const laneLayer = document.createElementNS(NS, "g");
    this.topology.lanes.forEach((lane, i) => {
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", i * LANE_W); rect.setAttribute("y", 0);
      rect.setAttribute("width", LANE_W); rect.setAttribute("height", this.viewH);
      rect.setAttribute("fill", LANE_FILLS[i % LANE_FILLS.length]);
      laneLayer.appendChild(rect);
      // Lane divider
      if (i > 0) {
        const line = document.createElementNS(NS, "line");
        line.setAttribute("x1", i * LANE_W); line.setAttribute("y1", LANE_TITLE_H);
        line.setAttribute("x2", i * LANE_W); line.setAttribute("y2", this.viewH);
        line.setAttribute("stroke", "#1e293b"); line.setAttribute("stroke-width", "1");
        laneLayer.appendChild(line);
      }
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", i * LANE_W + LANE_W / 2); t.setAttribute("y", 26);
      t.setAttribute("fill", "#64748b"); t.setAttribute("text-anchor", "middle");
      t.setAttribute("font-size", "12.5"); t.setAttribute("font-weight", "700");
      t.setAttribute("letter-spacing", "1.8"); t.setAttribute("font-family", "system-ui, sans-serif");
      t.textContent = lane.title.toUpperCase();
      laneLayer.appendChild(t);
    });
    viewport.appendChild(laneLayer);

    // Edges
    const edgeLayer = document.createElementNS(NS, "g");
    edgeLayer.setAttribute("class", "flow-edges");
    this.edgeIndex = new Map();
    this.outEdgeIndex = new Map();
    for (const edge of this.topology.edges) {
      const a = this.nodeIndex.get(edge.from), b = this.nodeIndex.get(edge.to);
      if (!a || !b) continue;
      const path = this._edgePath(a, b);
      const el = document.createElementNS(NS, "path");
      const color = edge.kind === "fanout" ? "#3b82f6" : edge.kind === "join" ? "#10b981" : edge.kind === "route" ? "#a78bfa" : "#475569";
      el.setAttribute("d", path);
      el.setAttribute("fill", "none");
      el.setAttribute("stroke", color);
      el.setAttribute("stroke-width", edge.kind === "fanout" || edge.kind === "join" ? "1.8" : "1.4");
      el.setAttribute("stroke-dasharray", edge.kind === "route" ? "6 5" : "");
      el.setAttribute("opacity", "0.28");
      el.setAttribute("marker-end", "url(#arrow)");
      el.dataset.kind = edge.kind; el.dataset.from = edge.from; el.dataset.to = edge.to; el.dataset.color = color;
      edgeLayer.appendChild(el);
      if (!this.edgeIndex.has(edge.to)) this.edgeIndex.set(edge.to, []);
      this.edgeIndex.get(edge.to).push(el);
      if (!this.outEdgeIndex.has(edge.from)) this.outEdgeIndex.set(edge.from, []);
      this.outEdgeIndex.get(edge.from).push(el);
    }
    viewport.appendChild(edgeLayer);

    // Particle layer (above edges, below nodes)
    const particleLayer = document.createElementNS(NS, "g");
    particleLayer.setAttribute("class", "flow-particles");
    viewport.appendChild(particleLayer);
    this.particleLayer = particleLayer;
    this._startParticleLoop();

    // Nodes
    const nodeLayer = document.createElementNS(NS, "g");
    nodeLayer.setAttribute("class", "flow-nodes");
    this.nodeElements = new Map();
    for (const [id, n] of this.nodeIndex.entries()) {
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", "flow-node");
      g.setAttribute("transform", `translate(${n.x}, ${n.y})`);
      g.style.cursor = "pointer";
      // Background rect with gradient
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H);
      rect.setAttribute("rx", NODE_RX); rect.setAttribute("ry", NODE_RX);
      g.appendChild(rect);
      // Role icon
      const role = this.topology.roles?.[id] || n.role || "agent";
      const icon = ROLE_ICONS[role] || "○";
      const iconEl = document.createElementNS(NS, "text");
      iconEl.setAttribute("x", 16); iconEl.setAttribute("y", NODE_H / 2 + 5);
      iconEl.setAttribute("font-size", "16"); iconEl.setAttribute("text-anchor", "middle");
      iconEl.textContent = icon;
      g.appendChild(iconEl);
      // Label
      const label = document.createElementNS(NS, "text");
      label.setAttribute("x", 36); label.setAttribute("y", NODE_H / 2 + 4.5);
      label.setAttribute("font-size", "12.5"); label.setAttribute("font-family", "system-ui, sans-serif");
      label.setAttribute("font-weight", "500");
      label.textContent = n.label;
      g.appendChild(label);
      // Status dot (right side)
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", NODE_W - 14); dot.setAttribute("cy", NODE_H / 2);
      dot.setAttribute("r", 5); dot.setAttribute("class", "flow-node-dot");
      g.appendChild(dot);
      // Interactions
      g.addEventListener("mousedown", (ev) => ev.stopPropagation());
      g.addEventListener("click", (ev) => { ev.stopPropagation(); this._selectNode(id); });
      g.addEventListener("mouseenter", () => this._highlightNeighborhood(id));
      g.addEventListener("mouseleave", () => this._clearHighlight());
      nodeLayer.appendChild(g);
      this.nodeElements.set(id, { g, rect, icon: iconEl, label, dot });
    }
    viewport.appendChild(nodeLayer);

    // Minimap
    this._renderMinimap(svg);

    svg.addEventListener("click", (e) => {
      if (e.target === svg || (e.target.tagName === "rect" && e.target.parentElement?.classList?.contains?.("flow-lanes"))) {
        if (!this._didDrag) { this._sticky = false; this.selectedId = null; this._refreshNodes(); this._clearHighlight(); }
      }
      this._didDrag = false;
    });

    this.container.appendChild(svg);
    this.svg = svg;
    this._bindPanZoom();
    this._refreshNodes();
    requestAnimationFrame(() => this._fit());
  }

  // ── Minimap ──────────────────────────────────────────────────────────
  _renderMinimap(svg) {
    const mmW = 150, mmH = 80, pad = 8;
    const mm = document.createElement("div");
    mm.className = "flow-minimap";
    mm.style.cssText = `position:absolute;bottom:10px;right:10px;width:${mmW}px;height:${mmH}px;background:#0a0f1ccc;border:1px solid #1f2937;border-radius:8px;overflow:hidden;cursor:pointer;z-index:5;`;
    const mms = document.createElementNS(NS, "svg");
    mms.setAttribute("viewBox", `0 0 ${this.viewW} ${this.viewH}`);
    mms.setAttribute("width", String(mmW));
    mms.setAttribute("height", String(mmH));
    // Tiny lane rects
    for (let i = 0; i < this.topology.lanes.length; i++) {
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", i * LANE_W); r.setAttribute("y", 0);
      r.setAttribute("width", LANE_W); r.setAttribute("height", this.viewH);
      r.setAttribute("fill", LANE_FILLS[i % LANE_FILLS.length]);
      r.setAttribute("opacity", "0.6");
      mms.appendChild(r);
    }
    // Tiny nodes
    for (const [id, n] of this.nodeIndex.entries()) {
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", n.x); r.setAttribute("y", n.y);
      r.setAttribute("width", NODE_W); r.setAttribute("height", NODE_H);
      r.setAttribute("rx", 3); r.setAttribute("fill", "#334155");
      r.dataset.mmId = id;
      mms.appendChild(r);
    }
    // Viewport rect
    const vpr = document.createElementNS(NS, "rect");
    vpr.setAttribute("x", 0); vpr.setAttribute("y", 0);
    vpr.setAttribute("width", 40); vpr.setAttribute("height", 30);
    vpr.setAttribute("fill", "none"); vpr.setAttribute("stroke", "#60a5fa"); vpr.setAttribute("stroke-width", "2");
    vpr.setAttribute("rx", 3);
    mms.appendChild(vpr);
    this._minimapVp = vpr;
    mm.appendChild(mms);
    // Click to jump
    mm.addEventListener("click", (e) => {
      const rect = mm.getBoundingClientRect();
      const sx = (e.clientX - rect.left) / mmW;
      const sy = (e.clientY - rect.top) / mmH;
      const wx = sx * this.viewW, wy = sy * this.viewH;
      const svgRect = this.svg.getBoundingClientRect();
      const fitScale = Math.min(svgRect.width / this.viewW, svgRect.height / this.viewH);
      this.transform.tx = svgRect.width / 2 / fitScale - wx;
      this.transform.ty = svgRect.height / 2 / fitScale - wy;
      this._applyTransform();
    });
    this.container.appendChild(mm);
    this._minimap = mm;
  }

  _updateMinimap() {
    if (!this._minimapVp || !this.svg) return;
    const svgRect = this.svg.getBoundingClientRect();
    if (!svgRect.width) return;
    const fitScale = Math.min(svgRect.width / this.viewW, svgRect.height / this.viewH);
    const vx = -this.transform.tx, vy = -this.transform.ty;
    const vw = svgRect.width / fitScale / this.transform.k, vh = svgRect.height / fitScale / this.transform.k;
    this._minimapVp.setAttribute("x", vx);
    this._minimapVp.setAttribute("y", vy);
    this._minimapVp.setAttribute("width", vw);
    this._minimapVp.setAttribute("height", vh);
    // Color by status
    if (this._minimap) {
      const mmSvgs = this._minimap.querySelectorAll("rect[data-mm-id]");
      mmSvgs.forEach(r => {
        const s = this.statusMap.get(r.dataset.mmId);
        r.setAttribute("fill", s === "running" ? "#3b82f6" : s === "done" ? "#10b981" : s === "error" ? "#ef4444" : "#334155");
      });
    }
  }

  // ── Particle loop ────────────────────────────────────────────────────
  _startParticleLoop() {
    if (this._particleTimer) cancelAnimationFrame(this._particleTimer);
    const tick = () => {
      this._animateParticles();
      this._updateMinimap();
      this._particleTimer = requestAnimationFrame(tick);
    };
    this._particleTimer = requestAnimationFrame(tick);
  }

  _animateParticles() {
    // Create new particles for running nodes
    const running = [...this.statusMap.entries()].filter(([, s]) => s === "running").map(([id]) => id);
    for (const id of running) {
      const edges = this.edgeIndex.get(id) || [];
      for (const el of edges) {
        // Limit particle count per edge
        const existing = this.particleLayer.querySelectorAll(`[data-to="${CSS.escape(id)}"]`).length;
        if (existing >= PARTICLE_COUNT) continue;
        const particle = document.createElementNS(NS, "circle");
        particle.setAttribute("r", "3.5");
        particle.setAttribute("fill", "#60a5fa");
        particle.setAttribute("opacity", "0.9");
        particle.dataset.to = id;
        particle.dataset.progress = "0";
        particle.dataset.speed = String(0.008 + Math.random() * 0.02);
        const anim = document.createElementNS(NS, "animateMotion");
        anim.setAttribute("dur", "2.2s");
        anim.setAttribute("repeatCount", "3");
        anim.setAttribute("path", el.getAttribute("d"));
        anim.setAttribute("begin", "0s");
        particle.appendChild(anim);
        this.particleLayer.appendChild(particle);
        // Remove after animation
        setTimeout(() => { if (particle.parentNode) particle.remove(); }, 7000);
      }
    }
    // Fade completed particles
    for (const p of this.particleLayer.children) {
      const prog = parseFloat(p.dataset.progress || "0") + parseFloat(p.dataset.speed || "0.01");
      p.dataset.progress = String(prog);
      if (prog > 0.7) p.setAttribute("opacity", String(Math.max(0, 0.9 * (1 - prog) / 0.3)));
    }
  }

  // ── Pan & zoom ───────────────────────────────────────────────────────
  _applyTransform() {
    const { tx, ty, k } = this.transform;
    this.viewport.setAttribute("transform", `translate(${tx} ${ty}) scale(${k})`);
  }
  _clientToWorld(cx, cy) {
    const rect = this.svg.getBoundingClientRect();
    const fitScale = Math.min(rect.width / this.viewW, rect.height / this.viewH);
    const ox = (rect.width - this.viewW * fitScale) / 2, oy = (rect.height - this.viewH * fitScale) / 2;
    const sx = (cx - rect.left - ox) / fitScale, sy = (cy - rect.top - oy) / fitScale;
    return { wx: (sx - this.transform.tx) / this.transform.k, wy: (sy - this.transform.ty) / this.transform.k };
  }
  _zoomBy(factor, anchor) {
    const newK = Math.max(0.2, Math.min(3, this.transform.k * factor));
    if (newK === this.transform.k) return;
    const p = this._clientToWorld(anchor.x, anchor.y);
    const rect = this.svg.getBoundingClientRect();
    const fitScale = Math.min(rect.width / this.viewW, rect.height / this.viewH);
    const sx = (anchor.x - rect.left - (rect.width - this.viewW * fitScale) / 2) / fitScale;
    const sy = (anchor.y - rect.top - (rect.height - this.viewH * fitScale) / 2) / fitScale;
    this.transform.k = newK;
    this.transform.tx = sx - p.wx * newK;
    this.transform.ty = sy - p.wy * newK;
    this._applyTransform();
  }
  _centerInClient() { const r = this.svg.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }
  _fit() { this.transform = { tx: 0, ty: 0, k: 1 }; this._applyTransform(); }
  _bindPanZoom() {
    let dragging = false, last = { x: 0, y: 0 };
    this.svg.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return; dragging = true; last = { x: e.clientX, y: e.clientY }; this.svg.style.cursor = "grabbing";
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - last.x, dy = e.clientY - last.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this._didDrag = true;
      last = { x: e.clientX, y: e.clientY };
      const rect = this.svg.getBoundingClientRect();
      const fitScale = Math.min(rect.width / this.viewW, rect.height / this.viewH);
      this.transform.tx += dx / fitScale; this.transform.ty += dy / fitScale;
      this._applyTransform();
    });
    window.addEventListener("mouseup", () => { if (!dragging) return; dragging = false; this.svg.style.cursor = "grab"; });
    this.svg.addEventListener("wheel", (e) => { e.preventDefault(); this._zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, { x: e.clientX, y: e.clientY }); }, { passive: false });
  }

  // ── Edges ────────────────────────────────────────────────────────────
  _edgePath(a, b) {
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
    const x2 = b.x, y2 = b.y + NODE_H / 2;
    if (b.x <= a.x) {
      const top = Math.min(y1, y2) - 42;
      return `M ${x1} ${y1} C ${x1 + 30} ${top}, ${x2 - 30} ${top}, ${x2} ${y2}`;
    }
    const dx = (x2 - x1) / 2;
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }

  // ── Node rendering ───────────────────────────────────────────────────
  _refreshNodes() { for (const id of this.nodeElements.keys()) this._refreshNode(id); }
  _refreshNode(id) {
    const el = this.nodeElements.get(id); if (!el) return;
    const status = this.statusMap.get(id) || "idle";
    const palette = STATUS_COLORS[status] || STATUS_COLORS.idle;
    const selected = id === this.selectedId;
    el.rect.setAttribute("fill", palette.fill);
    el.rect.setAttribute("stroke", palette.stroke);
    el.rect.setAttribute("stroke-width", selected ? "2.6" : status === "running" ? "1.6" : "1.2");
    el.label.setAttribute("fill", palette.text);
    el.icon.setAttribute("fill", palette.text);
    el.dot.setAttribute("fill", palette.stroke);
    // Glow filter
    if (status === "running") el.rect.setAttribute("filter", "url(#node-glow)");
    else if (status === "done") el.rect.setAttribute("filter", "url(#node-glow-done)");
    else if (status === "error") el.rect.setAttribute("filter", "url(#node-glow-error)");
    else el.rect.removeAttribute("filter");
  }

  _pulseEdgesInto(id) {
    const edges = this.edgeIndex.get(id) || [];
    for (const path of edges) {
      path.classList.remove("flow-edge-pulse");
      void path.getBoundingClientRect();
      path.classList.add("flow-edge-pulse");
      path.setAttribute("marker-end", "url(#arrow-active)");
      setTimeout(() => { path.classList.remove("flow-edge-pulse"); path.setAttribute("marker-end", "url(#arrow)"); }, 2000);
    }
  }

  // ── Selection & highlight ────────────────────────────────────────────
  _selectNode(id) { this.selectedId = id; this._refreshNodes(); this._renderDetail(id); this._highlightNeighborhood(id, true); }
  _highlightNeighborhood(id, sticky = false) {
    const neighbors = new Set([id]);
    const lit = new Set();
    for (const el of (this.outEdgeIndex.get(id) || [])) { lit.add(el); neighbors.add(el.dataset.to); }
    for (const el of (this.edgeIndex.get(id) || [])) { lit.add(el); neighbors.add(el.dataset.from); }
    for (const list of this.outEdgeIndex.values()) {
      for (const el of list) {
        const on = lit.has(el);
        el.setAttribute("opacity", String(on ? 0.95 : 0.12));
        el.setAttribute("stroke-width", on ? "2.4" : "1.0");
      }
    }
    for (const [nid, els] of this.nodeElements.entries()) {
      els.g.style.opacity = neighbors.has(nid) ? "1" : String(0.35);
    }
    this._sticky = sticky;
  }
  _clearHighlight() {
    if (this._sticky) return;
    for (const list of this.outEdgeIndex.values()) {
      for (const el of list) {
        el.setAttribute("opacity", "0.28");
        el.setAttribute("stroke-width", el.dataset.kind === "fanout" || el.dataset.kind === "join" ? "1.8" : "1.4");
      }
    }
    for (const els of this.nodeElements.values()) els.g.style.opacity = "1";
  }

  // ── Detail panel (structured) ────────────────────────────────────────
  _renderDetail(id) {
    if (!this.detail) return;
    const node = this.nodeIndex.get(id);
    const ev = this.lastEventFor.get(id);
    const status = this.statusMap.get(id) || "idle";
    const role = this.topology.roles?.[id] || "agent";
    const icon = ROLE_ICONS[role] || "○";
    const palette = STATUS_COLORS[status] || STATUS_COLORS.idle;
    const inputs = (this.topology.contextRoutes || {})[id] || [];
    const phase = node.phaseGroup || "";

    let h = `<div class="flow-detail-header">
      <span class="flow-detail-icon">${escapeHtml(icon)}</span>
      <span class="flow-detail-title">${escapeHtml(node.label)}</span>
      <span class="flow-pill flow-pill-${this._statusClass(status)}" style="margin-left:auto">${status.toUpperCase()}</span>
    </div>`;

    // Meta row
    h += `<div class="flow-detail-meta">`;
    h += `<span class="meta-chip">${escapeHtml(id)}</span>`;
    if (phase) h += `<span class="meta-chip">${escapeHtml(phase)}</span>`;
    h += `<span class="meta-chip">${escapeHtml(role)}</span>`;
    h += `</div>`;

    // Input state keys
    if (inputs.length) {
      h += `<h4 class="flow-detail-h4">↘ Input state keys</h4>`;
      h += `<ul class="chip-list">${inputs.map(k => `<li>${escapeHtml(k)}</li>`).join("")}</ul>`;
    }

    // Live status
    if (ev) {
      const stage = escapeHtml(ev.stage || "-");
      const detail = escapeHtml(String(ev.detail || ev.message || ev.error || "").slice(0, 300));
      const at = ev.at ? escapeHtml(ev.at.slice(11, 19)) : "";
      h += `<h4 class="flow-detail-h4">⚡ Sự kiện gần nhất</h4>`;
      h += `<div class="flow-detail-event">
        <div class="event-row"><span class="event-time">${at}</span><span class="event-stage">${stage}</span></div>
        <div class="event-detail">${detail}</div>
      </div>`;
    } else {
      h += `<p class="muted" style="margin-top:14px">Chưa có sự kiện cho node này.</p>`;
    }

    // Expandable raw
    if (ev) {
      h += `<details class="flow-detail-raw"><summary>Raw JSON</summary><pre>${escapeHtml(JSON.stringify(ev, null, 2))}</pre></details>`;
    }

    this.detail.innerHTML = h;
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

window.FlowView = FlowView;
})();
