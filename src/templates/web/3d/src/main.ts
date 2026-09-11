/** Scene graph and wiring only. Every number that matters lives in game.ts. */
import * as THREE from "three";
import { cameraTarget, initialState, PROPS, GROUND_HALF, step, type Input } from "./game";
import { telemetry } from "./bgate/telemetry";
import { tunables } from "./tunables";

const canvas = document.querySelector<HTMLCanvasElement>("#game");
if (!canvas) throw new Error("no #game canvas in the page");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1015);
scene.fog = new THREE.Fog(0x0e1015, 30, 70);

const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 200);

const sun = new THREE.DirectionalLight(0xfff0dd, 2.2);
sun.position.set(8, 14, 6);
sun.castShadow = true;
sun.shadow.mapSize.set(1024, 1024);
scene.add(sun, new THREE.HemisphereLight(0x8fb2ff, 0x2b2b33, 0.6));

const ground = new THREE.Mesh(
  new THREE.PlaneGeometry(GROUND_HALF * 2, GROUND_HALF * 2),
  new THREE.MeshStandardMaterial({ color: 0x2b3242, roughness: 0.95 }),
);
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

const PROP_COLORS = { platform: 0x3d4763, crate: 0x8a6a44, ramp: 0x4a5570 };
for (const p of PROPS) {
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(p.hx * 2, p.hy * 2, p.hz * 2),
    new THREE.MeshStandardMaterial({ color: PROP_COLORS[p.kind], roughness: 0.8 }),
  );
  mesh.position.set(p.x, p.y, p.z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);
}

// A capsule with its origin at the FEET, so game.ts can treat pos.y as ground
// height and never carry a half-height offset around.
const player = new THREE.Group();
const body = new THREE.Mesh(
  new THREE.CapsuleGeometry(0.4, 1.0, 4, 12),
  new THREE.MeshStandardMaterial({ color: 0xe4b363, roughness: 0.6 }),
);
body.position.y = 0.9;
body.castShadow = true;
const nose = new THREE.Mesh(
  new THREE.BoxGeometry(0.18, 0.18, 0.3),
  new THREE.MeshStandardMaterial({ color: 0x1b1d24 }),
);
nose.position.set(0, 1.35, 0.42);
player.add(body, nose);
scene.add(player);

const held = new Set<string>();
addEventListener("keydown", (e) => {
  held.add(e.code);
  if (["Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.code)) {
    e.preventDefault();
  }
});
addEventListener("keyup", (e) => held.delete(e.code));

function input(): Input {
  const on = (...codes: string[]) => (codes.some((c) => held.has(c)) ? 1 : 0);
  return {
    forward: on("KeyW", "ArrowUp") - on("KeyS", "ArrowDown"),
    strafe: on("KeyD", "ArrowRight") - on("KeyA", "ArrowLeft"),
    jump: held.has("Space"),
  };
}

function resize(): void {
  const w = innerWidth;
  const h = innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener("resize", resize);
resize();

const state = initialState();
camera.position.set(0, tunables.camera_height, tunables.camera_distance);

telemetry.start();
telemetry.emit("tunables", { ...tunables });

let last = performance.now();
function frame(now: number): void {
  const dt = (now - last) / 1000;
  last = now;

  for (const event of step(state, input(), dt)) {
    telemetry.emit(event.kind, event.data);
  }

  player.position.set(state.pos.x, state.pos.y, state.pos.z);
  player.rotation.y = state.yaw;

  // Framerate-independent smoothing. A raw lerp(a, b, lag) per frame makes the
  // camera stiffer at 144 Hz than at 60, so the feel changes with the monitor -
  // the kind of bug that gets reported as "it is different on my laptop" and
  // never reproduces on the machine it was written on.
  const want = cameraTarget(state);
  const k = 1 - Math.pow(1 - tunables.camera_lag, dt * 60);
  camera.position.lerp(new THREE.Vector3(want.x, want.y, want.z), k);
  camera.lookAt(state.pos.x, state.pos.y + 1.2, state.pos.z);

  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
