/**
 * 3D Agent Swarm Map — Three.js visualization of hierarchical agent tree.
 *
 * Requires Three.js loaded as global `THREE` (CDN script tag).
 */
class Swarm3DMap {
  constructor(container) {
    this.container = container;
    this.nodes = new Map();   // agentId → {data, mesh, ring}
    this.edges = new Map();   // edgeId → {line, from, to}
    this.selectedId = null;
    this.running = false;
    this.paused = false;
    this._animFrames = [];

    // Status colors (flat, no neon).
    this.colors = {
      queued: 0x64748B, running: 0x3B82F6, communicating: 0x8B5CF6,
      waiting: 0xF59E0B, blocked: 0xEF4444, failed: 0xDC2626,
      completed: 0x22C55E, testing: 0x06B6D4, fixing: 0xF97316,
    };

    this._initScene();
    this._initLights();
    this._initControls();
    this._initRaycaster();
    this._animate();
  }

  _initScene() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0A0F1C);
    this.scene.fog = new THREE.Fog(0x0A0F1C, 50, 200);

    this.camera = new THREE.PerspectiveCamera(55, w / h, 1, 500);
    this.camera.position.set(0, 30, 60);
    this.camera.lookAt(0, 0, 0);

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setSize(w, h);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.container.appendChild(this.renderer.domElement);

    // Grid floor
    const grid = new THREE.GridHelper(80, 40, 0x1E293B, 0x0F172A);
    this.scene.add(grid);

    // Root center marker
    const rootGeo = new THREE.RingGeometry(2, 2.3, 64);
    const rootMat = new THREE.MeshBasicMaterial({ color: 0x3B82F6, side: THREE.DoubleSide, transparent: true, opacity: 0.6 });
    this.rootRing = new THREE.Mesh(rootGeo, rootMat);
    this.rootRing.rotation.x = -Math.PI / 2;
    this.scene.add(this.rootRing);

    window.addEventListener('resize', () => this._onResize());
  }

  _initLights() {
    const ambient = new THREE.AmbientLight(0x334155, 1.2);
    this.scene.add(ambient);
    const dir = new THREE.DirectionalLight(0x94A3B8, 0.8);
    dir.position.set(10, 30, 20);
    this.scene.add(dir);
  }

  _initControls() {
    // Simple orbit via mouse drag
    this._isDragging = false;
    this._prevMouse = { x: 0, y: 0 };
    this._orbit = { theta: 0, phi: Math.PI / 3, radius: 60 };
    this._target = new THREE.Vector3(0, 0, 0);

    const el = this.renderer.domElement;
    el.addEventListener('mousedown', (e) => { this._isDragging = true; this._prevMouse = { x: e.clientX, y: e.clientY }; });
    el.addEventListener('mouseup', () => this._isDragging = false);
    el.addEventListener('mouseleave', () => this._isDragging = false);
    el.addEventListener('mousemove', (e) => {
      if (!this._isDragging) return;
      const dx = e.clientX - this._prevMouse.x;
      const dy = e.clientY - this._prevMouse.y;
      this._orbit.theta -= dx * 0.005;
      this._orbit.phi = Math.max(0.1, Math.min(Math.PI / 2, this._orbit.phi - dy * 0.005));
      this._prevMouse = { x: e.clientX, y: e.clientY };
    });
    el.addEventListener('wheel', (e) => {
      this._orbit.radius = Math.max(8, Math.min(150, this._orbit.radius + e.deltaY * 0.05));
    });
    // Touch
    el.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) { this._isDragging = true; this._prevMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY }; }
    });
    el.addEventListener('touchmove', (e) => {
      if (!this._isDragging || e.touches.length !== 1) return;
      const dx = e.touches[0].clientX - this._prevMouse.x;
      const dy = e.touches[0].clientY - this._prevMouse.y;
      this._orbit.theta -= dx * 0.005;
      this._orbit.phi = Math.max(0.1, Math.min(Math.PI / 2, this._orbit.phi - dy * 0.005));
      this._prevMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    });
    el.addEventListener('touchend', () => this._isDragging = false);
  }

  _initRaycaster() {
    this.raycaster = new THREE.Raycaster();
    this.raycaster.params.Points.threshold = 0.8;
    this._mouse = new THREE.Vector2();
    this.renderer.domElement.addEventListener('click', (e) => {
      const rect = this.renderer.domElement.getBoundingClientRect();
      this._mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      this._mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      this.raycaster.setFromCamera(this._mouse, this.camera);
      const meshes = [];
      this.nodes.forEach(n => { if (n.mesh) meshes.push(n.mesh); });
      const hits = this.raycaster.intersectObjects(meshes);
      if (hits.length > 0) {
        this._selectNode(hits[0].object.userData.agentId);
      }
    });
  }

  _animate() {
    const loop = () => {
      this._animFrames.push(requestAnimationFrame(loop));
      if (this.paused) return;
      // Orbit
      this._orbit.theta += 0.0003;
      const r = this._orbit.radius;
      const phi = this._orbit.phi;
      const theta = this._orbit.theta;
      this.camera.position.set(
        r * Math.sin(phi) * Math.cos(theta),
        r * Math.cos(phi),
        r * Math.sin(phi) * Math.sin(theta),
      );
      this.camera.lookAt(this._target);
      // Ring rotation
      if (this.rootRing) this.rootRing.rotation.z += 0.003;
      this.renderer.render(this.scene, this.camera);
    };
    loop();
  }

  _onResize() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  // ── Data ingestion ──────────────────────────────────────────────────

  loadTree(treeData) {
    if (!treeData || !treeData.agentId) return;
    this.nodes.clear();
    this.edges.clear();
    // Remove old meshes
    while (this.scene.children.length > 3) {
      const c = this.scene.children[this.scene.children.length - 1];
      if (c === this.rootRing || c.type === 'GridHelper' || c.type.includes('Light')) break;
      this.scene.remove(c);
    }
    this._buildSubtree(treeData, null, 0, 0);
    // Rebuild center ring
    this.rootRing.scale.setScalar(1 + this.nodes.size * 0.003);
  }

  _buildSubtree(node, parentId, depth, siblingIndex) {
    const { agentId, name, role, status, children, agentCount } = node;
    if (!agentId) return;

    // Position: Root at center, Leads in ring, Specialists around Lead.
    let pos;
    if (depth === 0) {
      pos = new THREE.Vector3(0, 0, 0);
    } else if (depth === 1) {
      const angle = (siblingIndex / Math.max(1, 20)) * Math.PI * 2;
      const radius = 15 + siblingIndex * 0.5;
      pos = new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius);
    } else {
      // Specialist: satellite around parent Lead.
      const parentPos = this.nodes.get(parentId)?.pos || new THREE.Vector3(0, 0, 0);
      const angle = (siblingIndex / Math.max(1, 10)) * Math.PI * 2;
      const radius = 4 + siblingIndex * 0.3;
      pos = parentPos.clone().add(new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius));
    }

    // Node sphere
    const size = depth === 0 ? 1.8 : depth === 1 ? 0.9 : 0.45;
    const geo = new THREE.SphereGeometry(size, 16, 12);
    const color = this.colors[status] || 0x64748B;
    const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.4, metalness: 0.3, emissive: color, emissiveIntensity: status === 'running' ? 0.5 : 0.05 });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.copy(pos);
    mesh.castShadow = true;
    mesh.userData = { agentId, name, role, status, depth };
    this.scene.add(mesh);

    // Label sprite
    const label = this._makeLabel(`${name||role}\n${status}`);
    label.position.copy(pos.clone().add(new THREE.Vector3(0, size + 0.6, 0)));
    this.scene.add(label);

    this.nodes.set(agentId, { data: node, mesh, pos, label, depth });

    // Edge to parent
    if (parentId && this.nodes.has(parentId)) {
      const parentPos = this.nodes.get(parentId).pos;
      const edgeGeo = new THREE.BufferGeometry().setFromPoints([parentPos, pos]);
      const edgeMat = new THREE.LineBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.4 });
      const edge = new THREE.Line(edgeGeo, edgeMat);
      this.scene.add(edge);
      this.edges.set(`${parentId}→${agentId}`, { line: edge, from: parentId, to: agentId });
    }

    // Recurse children
    if (children && Array.isArray(children)) {
      children.forEach((child, i) => this._buildSubtree(child, agentId, depth + 1, i));
    }
  }

  _makeLabel(text) {
    const canvas = document.createElement('canvas');
    canvas.width = 256; canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#FFFFFF';
    ctx.font = '14px system-ui, sans-serif';
    ctx.textAlign = 'center';
    const lines = text.split('\n');
    lines.forEach((l, i) => ctx.fillText(l, 128, 24 + i * 18));
    const tex = new THREE.CanvasTexture(canvas);
    tex.minFilter = THREE.LinearFilter;
    const spriteMat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
    const sprite = new THREE.Sprite(spriteMat);
    sprite.scale.set(4, 1, 1);
    return sprite;
  }

  _selectNode(agentId) {
    this.selectedId = agentId;
    const node = this.nodes.get(agentId);
    if (!node) return;
    // Focus camera
    this._target.copy(node.pos);
    // Emit event for inspector
    const ev = new CustomEvent('swarm:select', { detail: node.data });
    window.dispatchEvent(ev);
    // Highlight selected
    this.nodes.forEach((n, id) => {
      if (n.mesh && n.mesh.material) {
        n.mesh.material.emissiveIntensity = id === agentId ? 0.8 : statusEmissive(n.data.status);
      }
    });
  }

  // ── A2A message pulse ───────────────────────────────────────────────

  pulseEdge(fromId, toId) {
    const edge = this.edges.get(`${fromId}→${toId}`);
    if (!edge) return;
    const orig = edge.line.material.opacity;
    edge.line.material.opacity = 1;
    edge.line.material.color.set(0x3B82F6);
    setTimeout(() => {
      edge.line.material.opacity = orig;
      edge.line.material.color.set(0x334155);
    }, 600);
  }

  // ── Helpers ──────────────────────────────────────────────────────────

  fitToScreen() {
    this._orbit.radius = 40;
    this._orbit.phi = Math.PI / 3;
    this._target.set(0, 0, 0);
  }

  focusAgent(agentId) {
    const node = this.nodes.get(agentId);
    if (node) this._target.copy(node.pos);
  }

  setPaused(p) { this.paused = p; }

  destroy() {
    this._animFrames.forEach(id => cancelAnimationFrame(id));
    this.renderer.dispose();
    this.scene.clear();
  }
}

function statusEmissive(s) {
  const map = { running: 0.5, failed: 0.3, blocked: 0.3, completed: 0.05 };
  return map[s] || 0.05;
}

// Export for browser
if (typeof window !== 'undefined') window.Swarm3DMap = Swarm3DMap;
