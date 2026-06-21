/**
 * 2D SVG Swarm Agent Map — lightweight, works in Electron without WebGL.
 *
 * Root Orchestrator at center, Lead agents in concentric rings, Specialist
 * agents as satellites around each Lead. A2A edges pulse on message events.
 * Click-to-inspect. Pure SVG + vanilla JS — zero dependencies.
 */
class SwarmMap {
  constructor(container) {
    this.container = container;
    this.nodes = new Map();   // agentId → {data, x, y, depth}
    this.edges = new Map();   // edgeId → {from, to}
    this.selectedId = null;
    this._w = container.clientWidth || 800;
    this._h = container.clientHeight || 500;
    this._cx = this._w / 2;
    this._cy = this._h / 2;
    this._svg = null;
    this._tooltip = null;
    this._init();
  }

  _init() {
    this._w = this.container.clientWidth || 800;
    this._h = this.container.clientHeight || 500;
    this._cx = this._w / 2;
    this._cy = this._h / 2;

    this._svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this._svg.setAttribute("width", "100%");
    this._svg.setAttribute("height", "100%");
    this._svg.setAttribute("viewBox", `0 0 ${this._w} ${this._h}`);
    this._svg.style.background = "#0A0F1C";
    this._svg.style.fontFamily = "system-ui, sans-serif";
    this._svg.style.cursor = "grab";
    this.container.innerHTML = "";
    this.container.appendChild(this._svg);

    // Grid
    for (let i = 1; i <= 3; i++) {
      const r = i * 80;
      const circ = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circ.setAttribute("cx", this._cx);
      circ.setAttribute("cy", this._cy);
      circ.setAttribute("r", r);
      circ.setAttribute("fill", "none");
      circ.setAttribute("stroke", "#1E293B");
      circ.setAttribute("stroke-width", "1");
      circ.setAttribute("opacity", "0.4");
      this._svg.appendChild(circ);
    }

    // Tooltip
    this._tooltip = document.createElement("div");
    this._tooltip.style.cssText = "position:absolute;display:none;background:rgba(15,23,42,0.95);color:#E2E8F0;padding:8px 12px;border-radius:6px;font-size:11px;pointer-events:none;z-index:20;border:1px solid #334155;max-width:280px";
    this.container.style.position = "relative";
    this.container.appendChild(this._tooltip);

    // Click to inspect
    this._svg.addEventListener("click", (e) => {
      const target = e.target.closest("[data-agent-id]");
      if (!target) return;
      this._selectNode(target.getAttribute("data-agent-id"));
    });

    // Pan with drag
    let _dragging = false, _sx = 0, _sy = 0, _px = 0, _py = 0;
    this._svg.addEventListener("mousedown", (e) => {
      if (e.target.closest("[data-agent-id]")) return; // don't drag on node click
      _dragging = true; _sx = e.clientX; _sy = e.clientY;
      const vb = this._svg.getAttribute("viewBox").split(" ").map(Number);
      _px = vb[0] || 0; _py = vb[1] || 0;
    });
    window.addEventListener("mousemove", (e) => {
      if (!_dragging) return;
      const dx = e.clientX - _sx;
      const dy = e.clientY - _sy;
      const scale = (this._w / this.container.clientWidth);
      this._svg.setAttribute("viewBox", `${_px - dx * scale} ${_py - dy * scale} ${this._w} ${this._h}`);
    });
    window.addEventListener("mouseup", () => { _dragging = false; });

    // Zoom with scroll
    this._svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const vb = this._svg.getAttribute("viewBox").split(" ").map(Number);
      const z = e.deltaY > 0 ? 1.1 : 0.9;
      const mx = (e.offsetX / this.container.clientWidth) * this._w;
      const my = (e.offsetY / this.container.clientHeight) * this._h;
      const nw = Math.max(200, Math.min(3000, this._w * z));
      const nh = Math.max(200, Math.min(3000, this._h * z));
      const nx = vb[0] + mx * (1 - z);
      const ny = vb[1] + my * (1 - z);
      this._svg.setAttribute("viewBox", `${nx} ${ny} ${nw} ${nh}`);
    });
  }

  _onResize() {
    if (this.container.clientWidth > 0) {
      this._w = this.container.clientWidth;
      this._h = this.container.clientHeight;
      this._cx = this._w / 2;
      this._cy = this._h / 2;
      this._svg.setAttribute("viewBox", `0 0 ${this._w} ${this._h}`);
    }
  }

  // ── Colors ──
  _color(status) {
    const c = {
      queued: "#64748B", running: "#3B82F6", communicating: "#8B5CF6",
      waiting: "#F59E0B", blocked: "#EF4444", failed: "#DC2626",
      completed: "#22C55E", testing: "#06B6D4", fixing: "#F97316",
    };
    return c[status] || "#64748B";
  }

  _radius2(depth) { return depth === 0 ? 22 : depth === 1 ? 12 : 6; }

  // ── Data ingestion ──
  loadTree(treeData) {
    if (!treeData || !treeData.agentId) return;
    // Clear old nodes/edges
    this._svg.querySelectorAll("[data-agent-id],[data-edge]").forEach(e => e.remove());
    this.nodes.clear();
    this.edges.clear();
    this._buildSubtree(treeData, null, 0, 0, 20); // max 20 siblings per level
  }

  _buildSubtree(node, parentId, depth, siblingIndex, maxSiblings) {
    const { agentId, name, role, status, children } = node;
    if (!agentId) return;

    // Position
    let x, y;
    if (depth === 0) {
      x = this._cx; y = this._cy;
    } else if (depth === 1) {
      const angle = (siblingIndex / Math.max(1, maxSiblings)) * Math.PI * 2;
      const radius = 90 + siblingIndex * 3;
      x = this._cx + Math.cos(angle) * radius;
      y = this._cy + Math.sin(angle) * radius;
    } else {
      const pn = this.nodes.get(parentId);
      const px = pn ? pn.x : this._cx;
      const py = pn ? pn.y : this._cy;
      const angle = (siblingIndex / Math.max(1, 10)) * Math.PI * 2;
      const radius = 28 + siblingIndex * 2;
      x = px + Math.cos(angle) * radius;
      y = py + Math.sin(angle) * radius;
    }

    const r = this._radius2(depth);
    const color = this._color(status);
    const glow = status === "running" ? `filter: drop-shadow(0 0 4px ${color})` : "";

    // Node group
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("data-agent-id", agentId);
    g.setAttribute("transform", `translate(${x},${y})`);
    g.style.cursor = "pointer";
    g.style[glow.includes("drop-shadow") ? "filter" : "opacity"] = glow.includes("drop-shadow") ? glow.split(":")[1].replace(/[()]/g,"") : "";

    // Circle
    const circ = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circ.setAttribute("r", r);
    circ.setAttribute("fill", color);
    circ.setAttribute("stroke", "#1E293B");
    circ.setAttribute("stroke-width", depth === 0 ? "2" : "1");
    circ.setAttribute("opacity", "0.85");
    g.appendChild(circ);

    // Status ring for running
    if (status === "running") {
      const ring = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      ring.setAttribute("r", r + 3);
      ring.setAttribute("fill", "none");
      ring.setAttribute("stroke", color);
      ring.setAttribute("stroke-width", "1.5");
      ring.setAttribute("stroke-dasharray", "6 3");
      ring.setAttribute("opacity", "0.6");
      ring.innerHTML = `<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="4s" repeatCount="indefinite"/>`;
      g.appendChild(ring);
    }

    // Label
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("y", r + 13);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("fill", "#94A3B8");
    label.setAttribute("font-size", depth === 0 ? "11" : "9");
    label.textContent = (name || role || agentId.slice(-6)).slice(0, 18);
    g.appendChild(label);

    // Status badge
    if (status !== "queued") {
      const badge = document.createElementNS("http://www.w3.org/2000/svg", "text");
      badge.setAttribute("y", -r - 4);
      badge.setAttribute("text-anchor", "middle");
      badge.setAttribute("fill", color);
      badge.setAttribute("font-size", "7");
      badge.textContent = status;
      g.appendChild(badge);
    }

    // Hover tooltip
    g.addEventListener("mouseenter", (ev) => {
      this._tooltip.style.display = "block";
      this._tooltip.style.left = (ev.offsetX + 10) + "px";
      this._tooltip.style.top = (ev.offsetY - 30) + "px";
      this._tooltip.innerHTML = `<b>${name||agentId}</b><br>role: ${role}<br>status: ${status}<br>depth: ${depth}`;
    });
    g.addEventListener("mouseleave", () => { this._tooltip.style.display = "none"; });

    this._svg.appendChild(g);
    this.nodes.set(agentId, { data: node, x, y, depth, g });

    // Edge to parent
    if (parentId && this.nodes.has(parentId)) {
      const pp = this.nodes.get(parentId);
      const edge = document.createElementNS("http://www.w3.org/2000/svg", "line");
      edge.setAttribute("x1", pp.x); edge.setAttribute("y1", pp.y);
      edge.setAttribute("x2", x); edge.setAttribute("y2", y);
      edge.setAttribute("stroke", "#334155");
      edge.setAttribute("stroke-width", depth === 1 ? "1.2" : "0.7");
      edge.setAttribute("opacity", "0.5");
      edge.setAttribute("data-edge", `${parentId}→${agentId}`);
      this._svg.insertBefore(edge, this._svg.firstChild); // edges behind nodes
      this.edges.set(`${parentId}→${agentId}`, { edge, from: parentId, to: agentId });
    }

    // Children
    if (children && Array.isArray(children)) {
      children.forEach((c, i) => this._buildSubtree(c, agentId, depth + 1, i, children.length));
    }
  }

  _selectNode(agentId) {
    this.selectedId = agentId;
    const nd = this.nodes.get(agentId);
    if (!nd) return;
    // Dispatch event for inspector
    window.dispatchEvent(new CustomEvent("swarm:select", { detail: nd.data }));
    // Highlight selected
    this._svg.querySelectorAll("[data-agent-id] circle:first-child").forEach(c => {
      c.setAttribute("stroke", "#1E293B");
      c.setAttribute("stroke-width", c.getAttribute("r") > 15 ? "2" : "1");
    });
    const sel = this._svg.querySelector(`[data-agent-id="${agentId}"] circle:first-child`);
    if (sel) { sel.setAttribute("stroke", "#F8FAFC"); sel.setAttribute("stroke-width", "2.5"); }
  }

  // ── A2A edge pulse ──
  pulseEdge(fromId, toId) {
    const e = this.edges.get(`${fromId}→${toId}`);
    if (!e || !e.edge) return;
    e.edge.setAttribute("stroke", "#60A5FA");
    e.edge.setAttribute("opacity", "1");
    setTimeout(() => { e.edge.setAttribute("stroke", "#334155"); e.edge.setAttribute("opacity", "0.5"); }, 800);
  }

  fitToScreen() {
    this._svg.setAttribute("viewBox", `0 0 ${this._w} ${this._h}`);
  }

  destroy() {
    this.container.innerHTML = "";
  }
}

if (typeof window !== "undefined") window.SwarmMap = SwarmMap;
window.Swarm3DMap = SwarmMap; // backward compat
