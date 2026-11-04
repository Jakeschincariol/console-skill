import * as THREE from "three";

const mount = document.querySelector("#scene");
const toast = document.querySelector("#toast");
const clockLabel = document.querySelector("#clock");
const coinsLabel = document.querySelector("#coins");
const energyLabel = document.querySelector("#energy-value");
const energyBar = document.querySelector("#energy-bar");
const actionHint = document.querySelector("#action-hint");
const questProgress = document.querySelector("#quest-progress");
const questCopy = document.querySelector("#quest-copy");
const playerCard = document.querySelector("#player-card");
const touchAction = document.querySelector("#touch-action");
const toolButtons = [...document.querySelectorAll(".tool")];

const scene = new THREE.Scene();
scene.background = new THREE.Color("#a8b7a0");
scene.fog = new THREE.Fog("#a8b7a0", 25, 58);

const camera = new THREE.OrthographicCamera(-16, 16, 10, -10, 0.1, 120);
const cameraOffset = new THREE.Vector3(17, 22, 23);
const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.12;
mount.appendChild(renderer.domElement);

scene.add(new THREE.HemisphereLight("#e7efd7", "#625345", 2.25));
const sunlight = new THREE.DirectionalLight("#ffe5ad", 3.2);
sunlight.position.set(-12, 22, 8);
sunlight.castShadow = true;
sunlight.shadow.mapSize.set(2048, 2048);
sunlight.shadow.camera.left = -24;
sunlight.shadow.camera.right = 24;
sunlight.shadow.camera.top = 24;
sunlight.shadow.camera.bottom = -24;
sunlight.shadow.bias = -0.0004;
scene.add(sunlight);

const materials = {
  grass: new THREE.MeshStandardMaterial({ color: "#708957", roughness: 1 }),
  grassLight: new THREE.MeshStandardMaterial({ color: "#839866", roughness: 1 }),
  soil: new THREE.MeshStandardMaterial({ color: "#79583f", roughness: 1 }),
  watered: new THREE.MeshStandardMaterial({ color: "#4b4438", roughness: 1 }),
  stone: new THREE.MeshStandardMaterial({ color: "#b3a78b", roughness: 1 }),
  stoneLight: new THREE.MeshStandardMaterial({ color: "#d2c4a0", roughness: 1 }),
  wood: new THREE.MeshStandardMaterial({ color: "#79503a", roughness: 1 }),
  trim: new THREE.MeshStandardMaterial({ color: "#d5b879", roughness: 1 }),
  wall: new THREE.MeshStandardMaterial({ color: "#e5d6ad", roughness: 1 }),
  roof: new THREE.MeshStandardMaterial({ color: "#31545a", roughness: 1 }),
  leaf: new THREE.MeshStandardMaterial({ color: "#66804a", roughness: 1 }),
  leafLight: new THREE.MeshStandardMaterial({ color: "#81945a", roughness: 1 }),
  flower: new THREE.MeshStandardMaterial({ color: "#e9c2c3", roughness: 1 }),
  gold: new THREE.MeshStandardMaterial({ color: "#e6bc62", roughness: 0.75 }),
};

function box(parent, width, height, depth, material, position, options = {}) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, height, depth), material);
  mesh.position.set(...position);
  mesh.castShadow = options.castShadow !== false;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

function cylinder(parent, radiusTop, radiusBottom, height, material, position, sides = 8) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radiusTop, radiusBottom, height, sides), material);
  mesh.position.set(...position);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

function addGround() {
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(44, 44), materials.grass);
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.12;
  ground.receiveShadow = true;
  scene.add(ground);

  const meadow = new THREE.Group();
  for (let i = 0; i < 145; i += 1) {
    const x = (Math.random() - 0.5) * 40;
    const z = (Math.random() - 0.5) * 39;
    if (Math.abs(x) < 6.5 && z > -2 && z < 10) continue;
    const tuft = new THREE.Mesh(
      new THREE.BoxGeometry(0.12, 0.06, 0.18),
      i % 3 === 0 ? materials.grassLight : materials.leaf,
    );
    tuft.position.set(x, -0.07, z);
    tuft.rotation.y = Math.random() * Math.PI;
    meadow.add(tuft);
  }
  scene.add(meadow);
}

function addPath() {
  const path = new THREE.Group();
  const stoneGeometry = new THREE.CylinderGeometry(0.32, 0.36, 0.11, 6);
  const colors = ["#c2b393", "#a99e83", "#d0c19e", "#b6aa8d"];
  const stoneMaterials = colors.map((color) => new THREE.MeshStandardMaterial({ color, roughness: 1 }));
  for (let row = -11; row <= 13; row += 1) {
    for (let col = -8; col <= 8; col += 1) {
      const z = row * 0.72;
      const x = col * 0.75 + (row % 2) * 0.36;
      const plaza = z < -1 && z > -7 && Math.abs(x) < 8;
      const walkway = Math.abs(x) < 2.25 && z >= -1 && z < 9;
      if (!plaza && !walkway) continue;
      if (Math.abs(x) < 4.8 && z > 1.2 && z < 7.3) continue;
      const stone = new THREE.Mesh(stoneGeometry, stoneMaterials[Math.floor(Math.random() * stoneMaterials.length)]);
      stone.position.set(x + (Math.random() - 0.5) * 0.1, -0.035, z + (Math.random() - 0.5) * 0.1);
      stone.rotation.y = Math.random() * Math.PI;
      stone.scale.set(0.85 + Math.random() * 0.3, 0.7 + Math.random() * 0.45, 0.82 + Math.random() * 0.25);
      stone.receiveShadow = true;
      path.add(stone);
    }
  }
  scene.add(path);
}

function addHouse(x, z, scale, name) {
  const house = new THREE.Group();
  house.position.set(x, 0, z);
  house.scale.setScalar(scale);
  const width = 4;
  const depth = 3.3;
  box(house, width, 2.35, depth, materials.wall, [0, 1.18, 0]);

  const gableShape = new THREE.Shape();
  gableShape.moveTo(-2, 0);
  gableShape.lineTo(0, 1.8);
  gableShape.lineTo(2, 0);
  gableShape.closePath();
  const gable = new THREE.Mesh(new THREE.ShapeGeometry(gableShape), new THREE.MeshStandardMaterial({ color: "#cdbb92", side: THREE.DoubleSide }));
  gable.position.set(0, 2.35, depth / 2 + 0.015);
  gable.castShadow = true;
  house.add(gable);

  const leftRoof = box(house, 2.42, 0.2, depth + 0.45, materials.roof, [-1.12, 3.08, 0]);
  leftRoof.rotation.z = -0.67;
  const rightRoof = box(house, 2.42, 0.2, depth + 0.45, materials.roof, [1.12, 3.08, 0]);
  rightRoof.rotation.z = 0.67;
  box(house, 0.18, 0.2, depth + 0.6, materials.wood, [0, 3.84, 0]);

  box(house, 0.92, 1.52, 0.11, materials.wood, [0, 0.78, depth / 2 + 0.07]);
  box(house, 0.7, 0.08, 0.05, materials.trim, [0, 1.45, depth / 2 + 0.14]);
  for (const windowX of [-1.25, 1.25]) {
    box(house, 0.65, 0.72, 0.12, materials.roof, [windowX, 1.45, depth / 2 + 0.09]);
    box(house, 0.09, 0.83, 0.06, materials.trim, [windowX, 1.45, depth / 2 + 0.17]);
    box(house, 0.76, 0.08, 0.06, materials.trim, [windowX, 1.45, depth / 2 + 0.17]);
  }
  box(house, 0.09, 0.8, 0.1, materials.leaf, [2.05, 0.55, depth / 2 + 0.08]);
  house.userData.name = name;
  scene.add(house);
}

function addTree(x, z, size = 1) {
  const tree = new THREE.Group();
  tree.position.set(x, 0, z);
  tree.scale.setScalar(size);
  cylinder(tree, 0.22, 0.33, 1.65, materials.wood, [0, 0.78, 0], 7);
  const crown = new THREE.Mesh(new THREE.IcosahedronGeometry(1.16, 1), materials.leaf);
  crown.position.set(0, 2.08, 0);
  crown.scale.set(1, 1.08, 0.94);
  crown.castShadow = true;
  tree.add(crown);
  const crownTwo = new THREE.Mesh(new THREE.IcosahedronGeometry(0.72, 1), materials.leafLight);
  crownTwo.position.set(0.48, 2.32, 0.24);
  crownTwo.castShadow = true;
  tree.add(crownTwo);
  scene.add(tree);
}

function addFlower(x, z, colorMaterial = materials.flower) {
  const flower = new THREE.Group();
  flower.position.set(x, 0, z);
  cylinder(flower, 0.025, 0.03, 0.32, materials.leaf, [0, 0.16, 0], 5);
  const petals = new THREE.Mesh(new THREE.DodecahedronGeometry(0.13, 0), colorMaterial);
  petals.position.y = 0.35;
  petals.castShadow = true;
  flower.add(petals);
  scene.add(flower);
}

function addFence() {
  const fence = new THREE.Group();
  for (let x = -5.3; x <= 5.3; x += 1.05) {
    box(fence, 0.12, 0.75, 0.12, materials.wood, [x, 0.38, 7.1]);
    if (x < 5.3) box(fence, 1.05, 0.1, 0.1, materials.trim, [x + 0.52, 0.25, 7.1]);
    if (x < 5.3) box(fence, 1.05, 0.1, 0.1, materials.trim, [x + 0.52, 0.52, 7.1]);
  }
  scene.add(fence);
}

function makePlayer() {
  const player = new THREE.Group();
  const skin = new THREE.MeshStandardMaterial({ color: "#e7bb91", roughness: 1 });
  const shirt = new THREE.MeshStandardMaterial({ color: "#d1784d", roughness: 1 });
  const pants = new THREE.MeshStandardMaterial({ color: "#536d62", roughness: 1 });
  cylinder(player, 0.34, 0.38, 0.62, shirt, [0, 0.78, 0], 7);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.27, 8, 6), skin);
  head.position.set(0, 1.32, 0);
  head.castShadow = true;
  player.add(head);
  const hair = new THREE.Mesh(new THREE.SphereGeometry(0.28, 8, 5, 0, Math.PI * 2, 0, Math.PI * 0.55), materials.wood);
  hair.position.set(0, 1.42, 0);
  hair.castShadow = true;
  player.add(hair);
  box(player, 0.19, 0.38, 0.22, pants, [-0.13, 0.27, 0]);
  box(player, 0.19, 0.38, 0.22, pants, [0.13, 0.27, 0]);
  player.position.set(0, 0, 4.8);
  player.userData.speed = 5.2;
  scene.add(player);
  return player;
}

function createCrop(tile) {
  if (tile.crop) {
    scene.remove(tile.crop);
    tile.crop = null;
  }
  if (!tile.state) return;
  const crop = new THREE.Group();
  crop.position.set(tile.x, 0.13, tile.z);
  const height = tile.state === "ready" ? 0.92 : tile.state === "watered" ? 0.67 : 0.48;
  cylinder(crop, 0.035, 0.05, height, materials.leaf, [0, height / 2, 0], 5);
  for (let i = 0; i < 3; i += 1) {
    const leaf = new THREE.Mesh(
      new THREE.IcosahedronGeometry(tile.state === "ready" ? 0.24 : 0.17, 0),
      tile.state === "ready" ? materials.leafLight : materials.leaf,
    );
    leaf.position.set((i - 1) * 0.13, height * 0.72, (i % 2) * 0.13 - 0.06);
    leaf.scale.set(1.3, 0.6, 0.65);
    leaf.castShadow = true;
    crop.add(leaf);
  }
  if (tile.state === "ready") {
    const berry = new THREE.Mesh(new THREE.SphereGeometry(0.14, 7, 6), materials.gold);
    berry.position.set(0.11, height + 0.08, 0.06);
    berry.castShadow = true;
    crop.add(berry);
  }
  tile.crop = crop;
  scene.add(crop);
}

function makeFarm() {
  const tiles = [];
  const startX = -4.25;
  const startZ = 2.15;
  for (let row = 0; row < 3; row += 1) {
    for (let col = 0; col < 4; col += 1) {
      const x = startX + col * 1.05;
      const z = startZ + row * 1.05;
      const soil = new THREE.Mesh(new THREE.BoxGeometry(0.92, 0.14, 0.92), materials.soil);
      soil.position.set(x, 0.03, z);
      soil.receiveShadow = true;
      soil.userData.tileIndex = tiles.length;
      scene.add(soil);
      const tile = { x, z, soil, state: null, crop: null, readyAt: 0 };
      tiles.push(tile);
    }
  }
  return tiles;
}

function decorateTown() {
  addHouse(-5.4, -5.1, 0.95, "Casa da praça");
  addHouse(2.2, -6.2, 1.12, "Armazém");
  addHouse(8.1, -3.4, 0.78, "Florista");
  addHouse(-10.2, -2.3, 0.7, "Chalé");
  for (const [x, z, size] of [
    [-12, -9, 1.2], [-10, 3, 1.05], [11, -9, 1.15], [12, 2, 1.25],
    [8.8, 7, 0.92], [-8, 9.5, 1.1], [4, -11, 0.92], [-1, -11, 0.8],
  ]) addTree(x, z, size);
  for (const [x, z] of [[-6.4, 0.2], [-5.8, 0.1], [5.4, -0.2], [6.1, -0.2], [-8.7, 5.2], [8, 4.4], [4.6, -8]]) {
    addFlower(x, z);
  }
  addFence();
}

addGround();
addPath();
decorateTown();
const tiles = makeFarm();
const player = makePlayer();
const clock = new THREE.Clock();
const keys = new Set();
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let activeTile = null;
let selectedTool = "seeds";
let coins = 120;
let energy = 100;
let elapsed = 0;
let toastTimer;
let questDone = false;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 2100);
}

function setEnergy(value) {
  energy = Math.max(0, Math.min(100, value));
  energyLabel.textContent = `${energy} / 100`;
  energyBar.style.transform = `scaleX(${energy / 100})`;
}

function nearestTile() {
  let nearest = null;
  let distance = 1.65;
  for (const tile of tiles) {
    const next = Math.hypot(player.position.x - tile.x, player.position.z - tile.z);
    if (next < distance) {
      nearest = tile;
      distance = next;
    }
  }
  return nearest;
}

function updateHint() {
  activeTile = nearestTile();
  let hint;
  if (!activeTile) {
    hint = "Aproxime-se de um canteiro";
  } else if (activeTile.state === "watered") {
    hint = "Aguarde a colheita";
  } else if (activeTile.state === "ready") {
    hint = selectedTool === "hoe" ? "Colher a fruta" : "Selecione a enxada para colher";
  } else if (activeTile.state === "planted") {
    hint = selectedTool === "watering" ? "Regar a plantinha" : "Selecione o regador para regar";
  } else {
    hint = selectedTool === "seeds" ? "Plantar uma semente" : "Selecione sementes para plantar";
  }
  if (actionHint.textContent !== hint) actionHint.textContent = hint;
}

function chooseTool(tool) {
  selectedTool = tool;
  for (const button of toolButtons) {
    button.classList.toggle("active", button.dataset.tool === tool);
  }
  updateHint();
}

function interact(tile = activeTile) {
  if (!tile) {
    showToast("Chegue perto de um canteiro para cuidar da plantação.");
    return;
  }
  if (energy < 8) {
    showToast("Você está cansada. Descanse um pouco e tente de novo.");
    return;
  }
  if (!tile.state) {
    if (selectedTool !== "seeds") {
      showToast("Selecione as sementes para plantar.");
      return;
    }
    tile.state = "planted";
    createCrop(tile);
    setEnergy(energy - 8);
    showToast("Semente plantada! Agora regue o canteiro.");
    return;
  }
  if (tile.state === "planted") {
    if (selectedTool !== "watering") {
      showToast("Selecione o regador para cuidar da plantinha.");
      return;
    }
    tile.state = "watered";
    tile.readyAt = elapsed + 18;
    tile.soil.material = materials.watered;
    createCrop(tile);
    setEnergy(energy - 6);
    showToast("Regadinha! Sua plantinha está crescendo.");
    return;
  }
  if (tile.state === "watered") {
    showToast("A plantinha ainda está crescendo. Cuide de outro canteiro!");
    return;
  }
  if (selectedTool !== "hoe") {
    showToast("Selecione a enxada para colher.");
    return;
  }
  tile.state = null;
  tile.soil.material = materials.soil;
  createCrop(tile);
  coins += 25;
  coinsLabel.textContent = String(coins);
  setEnergy(energy - 4);
  showToast("Colheita fresquinha! +25 moedas");
  if (!questDone) {
    questDone = true;
    questProgress.style.transform = "scaleX(1)";
    questCopy.textContent = "Primeira colheita feita! Seu sítio já está florescendo.";
    showToast("Missão concluída: um cantinho só seu! +25 moedas");
  }
}

function updateCrops() {
  for (const tile of tiles) {
    if (tile.state === "watered" && elapsed >= tile.readyAt) {
      tile.state = "ready";
      createCrop(tile);
      showToast("Uma plantinha está pronta para colher!");
    }
  }
}

function updateClock() {
  const minutes = Math.floor(elapsed * 1.7);
  const hour = 6 + Math.floor(minutes / 60);
  const minute = minutes % 60;
  clockLabel.textContent = `${String(Math.min(hour, 22)).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

function movePlayer(delta) {
  const direction = new THREE.Vector3(
    Number(keys.has("d") || keys.has("arrowright")) - Number(keys.has("a") || keys.has("arrowleft")),
    0,
    Number(keys.has("s") || keys.has("arrowdown")) - Number(keys.has("w") || keys.has("arrowup")),
  );
  if (direction.lengthSq() === 0) return;
  direction.normalize();
  player.position.addScaledVector(direction, player.userData.speed * delta);
  player.position.x = THREE.MathUtils.clamp(player.position.x, -18, 18);
  player.position.z = THREE.MathUtils.clamp(player.position.z, -16, 15);
  player.rotation.y = Math.atan2(direction.x, direction.z);
}

function animatePlayerLabel() {
  const screenPosition = player.position.clone().add(new THREE.Vector3(0, 2, 0)).project(camera);
  const x = (screenPosition.x * 0.5 + 0.5) * window.innerWidth;
  const y = (-screenPosition.y * 0.5 + 0.5) * window.innerHeight;
  playerCard.style.transform = `translate(-50%, -50%) translate(${x}px, ${y}px)`;
}

function tick() {
  const delta = Math.min(clock.getDelta(), 0.05);
  elapsed += delta;
  movePlayer(delta);
  updateCrops();
  updateClock();
  updateHint();
  const target = player.position.clone().add(cameraOffset);
  camera.position.lerp(target, 1 - Math.exp(-delta * 3.8));
  camera.lookAt(player.position.x, 0, player.position.z);
  animatePlayerLabel();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

function resize() {
  const width = window.innerWidth;
  const height = window.innerHeight;
  const aspect = width / height;
  const viewHeight = width < 700 ? 25 : 19;
  camera.left = -viewHeight * aspect / 2;
  camera.right = viewHeight * aspect / 2;
  camera.top = viewHeight / 2;
  camera.bottom = -viewHeight / 2;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
}

window.addEventListener("keydown", (event) => {
  const key = event.key.toLowerCase();
  if (["arrowup", "arrowdown", "arrowleft", "arrowright", " "].includes(key)) event.preventDefault();
  keys.add(key);
  if (key === "1" || key === "2" || key === "3") {
    const tool = toolButtons[Number(key) - 1].dataset.tool;
    chooseTool(tool);
  }
  if (key === "e" && !event.repeat) interact();
});
window.addEventListener("keyup", (event) => keys.delete(event.key.toLowerCase()));
window.addEventListener("blur", () => keys.clear());
window.addEventListener("resize", resize);
touchAction.addEventListener("click", () => interact());
toolButtons.forEach((button) => {
  button.addEventListener("click", () => chooseTool(button.dataset.tool));
});
renderer.domElement.addEventListener("pointerdown", (event) => {
  const bounds = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
  pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const soilMeshes = tiles.map((tile) => tile.soil);
  const [hit] = raycaster.intersectObjects(soilMeshes);
  if (hit && Math.hypot(player.position.x - hit.object.position.x, player.position.z - hit.object.position.z) < 2.4) {
    interact(tiles[hit.object.userData.tileIndex]);
  }
});

resize();
tick();
