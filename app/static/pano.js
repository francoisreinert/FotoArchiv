// FotoArchiv – 360°-Betrachter für equirektangulare Fotos (Photo Sphere, GoPro Fusion, Theta …).
// WebGL ohne fremde Bibliotheken: ein Rechteck über die ganze Fläche, der Fragment-Shader rechnet für jeden
// Bildpunkt die Blickrichtung in Länge/Breite um und liest die Farbe aus dem Panorama. Ziehen dreht,
// Mausrad/zwei Finger zoomen, Doppelklick setzt zurück. Bis zur ersten Bewegung dreht es sich langsam.

const PANO_VS = "attribute vec2 p; varying vec2 v; void main() { v = p; gl_Position = vec4(p, 0.0, 1.0); }";
const PANO_FS = `precision highp float;
varying vec2 v;
uniform sampler2D tex;
uniform float yaw, pitch, tanHalf, aspect;
uniform vec4 crop;   // links, oben, Breite, Höhe des Bildes im vollen Panorama (Anteile 0..1)
void main() {
  vec3 d = normalize(vec3(v.x * tanHalf * aspect, v.y * tanHalf, -1.0));
  float cp = cos(pitch), sp = sin(pitch);
  d = vec3(d.x, d.y * cp - d.z * sp, d.y * sp + d.z * cp);
  float cy = cos(yaw), sy = sin(yaw);
  d = vec3(d.x * cy - d.z * sy, d.y, d.x * sy + d.z * cy);
  float lon = atan(d.x, -d.z);
  float lat = asin(clamp(d.y, -1.0, 1.0));
  vec2 uv = vec2(lon / 6.2831853 + 0.5, 0.5 - lat / 3.1415927);
  vec2 t = (uv - crop.xy) / crop.zw;
  if (t.x < 0.0 || t.x > 1.0 || t.y < 0.0 || t.y > 1.0) gl_FragColor = vec4(0.07, 0.08, 0.09, 1.0);
  else gl_FragColor = texture2D(tex, t);
}`;

class PanoViewer {
  constructor(container, src, info) {
    this.info = info || {};
    this.yaw = 0;
    this.pitch = 0;
    this.fov = 75 * Math.PI / 180;
    this.auto = true;   // langsames Drehen bis zur ersten Berührung
    this.ptrs = new Map();
    this.canvas = document.createElement("canvas");
    this.canvas.className = "pano";
    container.appendChild(this.canvas);
    const gl = this.gl = this.canvas.getContext("webgl", { antialias: false, preserveDrawingBuffer: false });
    if (!gl) throw new Error("WebGL nicht verfügbar");
    const sh = (type, txt) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, txt);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, PANO_VS));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, PANO_FS));
    gl.linkProgram(prog);
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    this.u = {};
    for (const n of ["yaw", "pitch", "tanHalf", "aspect", "crop", "tex"]) this.u[n] = gl.getUniformLocation(prog, n);
    // Ausschnitt: Teilpanoramen liegen nur in einem Teil der Kugel
    const i = this.info;
    const fw = i.fw || i.cw || 2, fh = i.fh || fw / 2;
    gl.uniform4f(this.u.crop, (i.left || 0) / fw, (i.top || 0) / fh, (i.cw || fw) / fw, (i.ch || fh) / fh);
    this.ready = false;
    this.bind();
    const img = new Image();
    img.onload = () => { if (this.canvas.isConnected) this.setImage(img); };
    img.onerror = () => { if (this.onerror) this.onerror(); };
    img.src = src;
  }

  setImage(img) {
    const gl = this.gl;
    let source = img;
    const max = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    if (img.naturalWidth > max || img.naturalHeight > max) {  // zu groß für die Grafikkarte: verkleinern
      const k = max / Math.max(img.naturalWidth, img.naturalHeight);
      source = document.createElement("canvas");
      source.width = Math.floor(img.naturalWidth * k);
      source.height = Math.floor(img.naturalHeight * k);
      source.getContext("2d").drawImage(img, 0, 0, source.width, source.height);
    }
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, source);
    gl.uniform1i(this.u.tex, 0);
    this.ready = true;
    if (this.onload) this.onload();
    this.loop();
  }

  bind() {
    const c = this.canvas;
    const stop = e => e.stopPropagation();  // Wischen/Klicks gehören dem Panorama, nicht dem Blättern
    c.addEventListener("touchstart", stop, { passive: true });
    c.addEventListener("touchend", stop);
    c.addEventListener("pointerdown", e => {
      this.auto = false;
      c.setPointerCapture(e.pointerId);
      this.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      e.preventDefault();
    });
    c.addEventListener("pointermove", e => {
      const prev = this.ptrs.get(e.pointerId);
      if (!prev) return;
      if (this.ptrs.size === 2) {  // zwei Finger: zoomen
        const [a, b] = [...this.ptrs.values()];
        const before = Math.hypot(a.x - b.x, a.y - b.y);
        this.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
        const [a2, b2] = [...this.ptrs.values()];
        const after = Math.hypot(a2.x - b2.x, a2.y - b2.y);
        if (before > 0 && after > 0) this.zoom(before / after);
        return;
      }
      const rpp = this.fov / Math.max(1, c.clientHeight);  // Winkel je Bildschirmpunkt
      this.yaw -= (e.clientX - prev.x) * rpp;
      this.pitch = Math.max(-1.5, Math.min(1.5, this.pitch + (e.clientY - prev.y) * rpp));
      this.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      this.draw();
    });
    const up = e => this.ptrs.delete(e.pointerId);
    c.addEventListener("pointerup", up);
    c.addEventListener("pointercancel", up);
    c.addEventListener("wheel", e => { e.preventDefault(); this.auto = false; this.zoom(Math.exp(e.deltaY * 0.0012)); }, { passive: false });
    c.addEventListener("dblclick", e => { e.stopPropagation(); this.yaw = 0; this.pitch = 0; this.fov = 75 * Math.PI / 180; this.draw(); });
  }

  zoom(f) {
    this.fov = Math.max(25 * Math.PI / 180, Math.min(115 * Math.PI / 180, this.fov * f));
    this.draw();
  }

  loop() {
    if (!this.canvas.isConnected) return;  // Betrachter geschlossen oder anderes Foto: Schleife endet
    if (this.auto) this.yaw += 0.0012;
    this.draw();
    requestAnimationFrame(() => this.loop());
  }

  draw() {
    if (!this.ready || !this.canvas.isConnected) return;
    const gl = this.gl, c = this.canvas;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    gl.viewport(0, 0, w, h);
    gl.uniform1f(this.u.yaw, this.yaw);
    gl.uniform1f(this.u.pitch, this.pitch);
    gl.uniform1f(this.u.tanHalf, Math.tan(this.fov / 2));
    gl.uniform1f(this.u.aspect, w / Math.max(1, h));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
}
