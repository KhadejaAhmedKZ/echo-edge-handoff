import * as THREE from './three.module.min.js';
export function glowTexture(){
 const canvas=document.createElement('canvas');canvas.width=128;canvas.height=128;const ctx=canvas.getContext('2d');
 const grad=ctx.createRadialGradient(64,64,0,64,64,64);grad.addColorStop(0,'rgba(255,255,255,1)');grad.addColorStop(.09,'rgba(255,255,255,.95)');grad.addColorStop(.22,'rgba(255,255,255,.35)');grad.addColorStop(.5,'rgba(255,255,255,.08)');grad.addColorStop(1,'rgba(255,255,255,0)');ctx.fillStyle=grad;ctx.fillRect(0,0,128,128);return new THREE.CanvasTexture(canvas);
}
export const glowMap=glowTexture();
export function satelliteModel(role,color){
 const obj=new THREE.Group();
 const body=new THREE.Mesh(new THREE.BoxGeometry(.72,.48,.85),new THREE.MeshPhongMaterial({color:0xcbd5df,specular:0xffffff,shininess:70}));obj.add(body);
 const foil=new THREE.Mesh(new THREE.BoxGeometry(.52,.52,.52),new THREE.MeshPhongMaterial({color:0xcba45e,specular:0xffe4a0,shininess:35}));foil.position.z=.48;obj.add(foil);
 for(const sign of [-1,1]){
  const panel=new THREE.Mesh(new THREE.BoxGeometry(1.25,.06,.92),new THREE.MeshPhongMaterial({color:0x174874,emissive:0x041321,specular:0x9bdaff,shininess:80}));panel.position.x=sign*1.1;obj.add(panel);
  const frame=new THREE.LineSegments(new THREE.EdgesGeometry(panel.geometry),new THREE.LineBasicMaterial({color:0x82b5d0,transparent:true,opacity:.7}));frame.position.copy(panel.position);obj.add(frame);
  for(let i=0;i<4;i++){const grid=new THREE.Mesh(new THREE.BoxGeometry(.015,.075,.91),new THREE.MeshBasicMaterial({color:0x527ca1}));grid.position.set(sign*1.1-.47+i*.31,0,0);obj.add(grid)}
 }
 const halo=new THREE.Sprite(new THREE.SpriteMaterial({map:glowMap,color,transparent:true,opacity:role==='visible'?.38:.85,blending:THREE.AdditiveBlending,depthWrite:false}));halo.scale.setScalar(role==='visible'?3.5:7);obj.add(halo);
 obj.userData.halo=halo;obj.userData.glowSize=role==='visible'?3.5:7;
 obj.rotation.z=.25;
 return obj;
}
