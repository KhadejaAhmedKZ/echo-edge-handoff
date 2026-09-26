import * as THREE from './three.module.min.js';
import {satelliteModel} from './satellite-model.js';
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
let state = {obs:null, time:0, playing:false, handoff:{}};
window.addEventListener('echo-mission-view', e => { state = e.detail; });
const colors = {wifi:0xd4cfc3, cellular:0xe99a59, satellite:0xa2aca7, wired:0x8e969c};
const mat = (color, metalness=.6, roughness=.35) => new THREE.MeshStandardMaterial({color,metalness,roughness});
const glow = color => new THREE.MeshBasicMaterial({color});
function mesh(g,m,parent,x=0,y=0,z=0){const o=new THREE.Mesh(g,m);o.position.set(x,y,z);parent.add(o);return o;}
function box(p,w,h,d,m,x=0,y=0,z=0){return mesh(new THREE.BoxGeometry(w,h,d),m,p,x,y,z);}
function ball(p,r,m,x=0,y=0,z=0){return mesh(new THREE.SphereGeometry(r,24,16),m,p,x,y,z);}
function rod(p,a,b,r,m){const v=new THREE.Vector3(...b).sub(new THREE.Vector3(...a));const o=mesh(new THREE.CylinderGeometry(r,r,v.length(),12),m,p);o.position.copy(new THREE.Vector3(...a).addScaledVector(v,.5));o.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),v.normalize());return o;}
function setup(id){const c=document.getElementById(id);const renderer=new THREE.WebGLRenderer({canvas:c,antialias:true,alpha:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setClearColor(0,0);renderer.outputColorSpace=THREE.SRGBColorSpace;const scene=new THREE.Scene();const camera=new THREE.PerspectiveCamera(35,1,.1,100);scene.add(new THREE.HemisphereLight(0xeeeeea,0x20201e,2.0));const light=new THREE.DirectionalLight(0xfff4e3,2.2);light.position.set(-3,6,5);scene.add(light);const rim=new THREE.DirectionalLight(0xd5cdc0,1.1);rim.position.set(3,2,-4);scene.add(rim);const resize=()=>{const r=c.getBoundingClientRect();renderer.setSize(r.width,r.height,false);camera.aspect=r.width/r.height;camera.updateProjectionMatrix();};new ResizeObserver(resize).observe(c);resize();return {renderer,scene,camera,c};}
function drag(c,callback){let px=null;c.addEventListener('pointerdown',e=>{if(e.pointerType==='touch')return;px=e.clientX;c.setPointerCapture(e.pointerId);});c.addEventListener('pointermove',e=>{if(px!==null){callback((e.clientX-px)*.008);px=e.clientX;}});c.addEventListener('pointerup',()=>px=null);c.addEventListener('pointercancel',()=>px=null);}
try {
 const mini=setup('miniEarthCanvas');mini.camera.position.set(0,.2,7.9);mini.camera.lookAt(0,0,0);
 const earth=setup('earthCanvas');earth.camera.position.set(0,.2,7.9);earth.camera.lookAt(0,0,0);
 const globe=new THREE.Group();earth.scene.add(globe);globe.position.set(.25,-.05,0);globe.rotation.y=-1.2;globe.rotation.z=.14;
 const tex=new THREE.TextureLoader().load('assets/earth.jpg');tex.colorSpace=THREE.SRGBColorSpace;
 ball(globe,1.82,new THREE.MeshStandardMaterial({map:tex,roughness:1,metalness:.12}));
 const air=ball(globe,1.89,new THREE.ShaderMaterial({transparent:true,side:THREE.BackSide,uniforms:{},vertexShader:'varying vec3 n; varying vec3 v; void main(){vec4 p=modelViewMatrix*vec4(position,1.);n=normalize(normalMatrix*normal);v=normalize(-p.xyz);gl_Position=projectionMatrix*p;}',fragmentShader:'varying vec3 n;varying vec3 v;void main(){float a=pow(1.-abs(dot(n,v)),3.);gl_FragColor=vec4(.15,.55,1.,a*.65);}'}));
 // Reuse the old dashboard's satellite models and cached orbital catalog.
 const orbitGroup = new THREE.Group();globe.add(orbitGroup);
 function drawCatalog(data){
 orbitGroup.traverse(o=>{o.geometry?.dispose();if(o.material) o.material.dispose();});
 orbitGroup.clear();
   const positions=data.satellites.flatMap(s=>s.position);
   const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
   const points=new THREE.Points(geometry,new THREE.PointsMaterial({color:0x89dcff,size:.018,transparent:true,opacity:.85}));orbitGroup.add(points);
   data.orbits.forEach((o,i)=>{
     const geo=new THREE.BufferGeometry().setFromPoints(o.points.map(p=>new THREE.Vector3(...p)));
     orbitGroup.add(new THREE.Line(geo,new THREE.LineBasicMaterial({color:[0x66e6d3,0x8ebcff,0xf9bf72][i],transparent:true,opacity:.6})));
     const sat=satelliteModel('visible',[0x66e6d3,0x8ebcff,0xf9bf72][i]);sat.scale.setScalar(.16);globe.updateMatrixWorld(true);const front=o.points.reduce((a,b)=>new THREE.Vector3(...a).applyQuaternion(globe.quaternion).z > new THREE.Vector3(...b).applyQuaternion(globe.quaternion).z ? a:b);sat.position.fromArray(front);sat.lookAt(0,0,0);orbitGroup.add(sat);
   });
   document.getElementById('orbitCaption').textContent = data.satellites.length.toLocaleString()+' satellites · cached 21 Sep 2026 · not live';
 }
 fetch('assets/constellation.json').then(r=>r.json()).then(drawCatalog).catch(()=>{document.getElementById('orbitCaption').textContent='Satellite catalog unavailable';});
 const trackedGroup=new THREE.Group();globe.add(trackedGroup);
 function orbitalPosition(s){const a=s.lat*Math.PI/180,b=s.lng*Math.PI/180,r=1.82*(1+s.altitude);return new THREE.Vector3(r*Math.cos(a)*Math.cos(b),r*Math.sin(a),-r*Math.cos(a)*Math.sin(b));}
 window.addEventListener('echo-orbit-targets',e=>{
   trackedGroup.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});trackedGroup.clear();
   const d=e.detail;const lat=24.4539*Math.PI/180,lon=54.3773*Math.PI/180;
   const ground=new THREE.Vector3(1.83*Math.cos(lat)*Math.cos(lon),1.83*Math.sin(lat),-1.83*Math.cos(lat)*Math.sin(lon));
   ball(trackedGroup,.022,glow(0xffa04a),...ground.toArray());
   for(const [s,color] of [[d.serving,0xffad45],[d.candidate,0x66e6d3]]){if(!s)continue;const position=orbitalPosition(s),sat=satelliteModel('visible',color);sat.scale.setScalar(.12);sat.position.copy(position);sat.lookAt(0,0,0);trackedGroup.add(sat);trackedGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([ground,position]),new THREE.LineBasicMaterial({color,transparent:true,opacity:.7})));}
 });
 window.addEventListener('echo-live-catalog',e=>{const d=e.detail;drawCatalog({satellites:d.satellites.map(s=>{const a=s.lat*Math.PI/180,b=s.lng*Math.PI/180,r=1.82*(1+s.altitude);return {position:[r*Math.cos(a)*Math.cos(b),r*Math.sin(a),-r*Math.cos(a)*Math.sin(b)]};}),orbits:[]});document.getElementById('orbitCaption').textContent=d.satellites.length.toLocaleString()+' positions propagated '+new Date(d.observedAt).toLocaleTimeString()+(d.catalogStale?' · stale elements':d.partial?' · partial catalog':' · refreshed catalog');});

 const dots=[];for(let i=0;i<150;i++){const a=i*2.39996;const y=1-i/75;const r=Math.sqrt(Math.max(0,1-y*y));dots.push(Math.cos(a)*r*6,y*5,Math.sin(a)*r*4-3);}const sg=new THREE.BufferGeometry();sg.setAttribute('position',new THREE.Float32BufferAttribute(dots,3));earth.scene.add(new THREE.Points(sg,new THREE.PointsMaterial({color:0x789cbc,size:.016,transparent:true,opacity:.7})));
 drag(earth.c,d=>globe.rotation.y+=d);
 document.getElementById('earthFocus').onclick=()=>{earth.camera.position.z=7.9;globe.rotation.y=-1.2;};
 document.getElementById('earthFocus').textContent='Recenter Earth';
 document.getElementById('earthCloser').onclick=()=>{earth.camera.position.z=Math.max(5.4,earth.camera.position.z-.7);};
 document.getElementById('earthFarther').onclick=()=>{earth.camera.position.z=Math.min(11,earth.camera.position.z+.7);};
 const rob=setup('robotCanvas');rob.camera.position.set(4.7,3.0,4.9);rob.camera.lookAt(0,.75,0);
 const site=new THREE.Group();rob.scene.add(site);
 const floor=mesh(new THREE.PlaneGeometry(40,30),mat(0x20201e,.1,.95),site);floor.rotation.x=-Math.PI/2;
 const grid=new THREE.GridHelper(40,65,0x393936,0x292927);grid.position.y=.01;site.add(grid);
 const ring=mesh(new THREE.TorusGeometry(1.65,.013,8,100),glow(0x777269),site,0,.025,0);ring.rotation.x=Math.PI/2;
 for(const z of [-1.1,1.1]){const lane=mesh(new THREE.PlaneGeometry(22,.07),glow(0x5b4c35),site,0,.02,z);lane.rotation.x=-Math.PI/2;}
 const road=mesh(new THREE.PlaneGeometry(22,2.2),mat(0x252523,.05,.95),site,0,.015,0);road.rotation.x=-Math.PI/2;
 const robot=new THREE.Group();site.add(robot);robot.scale.setScalar(1.12);
 const shell=mat(0xffcd19,.35,.36),dark=mat(0x292927,.8,.3),black=mat(0x111111,.35,.6),teal=glow(0xe5ddd0),orange=mat(0xffcd19,.35,.36);
 // Armoured torso, layered side panels and top-mounted range sensor.
 box(robot,1.6,.43,.68,shell,0,1.18,0);box(robot,1.38,.12,.61,dark,0,1.44,0);
 for(const z of [-.36,.36]){box(robot,1.08,.29,.045,shell,-.08,1.2,z);}
 box(robot,.3,.33,.54,orange,.64,1.19,0);box(robot,.19,.29,.46,black,.86,1.2,0);
 for(const z of [-.16,.16]){const eye=mesh(new THREE.CylinderGeometry(.072,.072,.03,24),teal,robot,.97,1.24,z);eye.rotation.z=Math.PI/2;}
 mesh(new THREE.CylinderGeometry(.19,.22,.11,32),dark,robot,-.15,1.55,0);mesh(new THREE.CylinderGeometry(.16,.16,.08,32),teal,robot,-.15,1.62,0);
 rod(robot,[-.53,1.42,0],[-.6,1.86,0],.018,dark);ball(robot,.036,teal,-.6,1.86,0);
 // Locally drawn EDGE wordmark on both yellow side panels.
 const label=document.createElement('canvas');label.width=512;label.height=128;
 const ctx=label.getContext('2d');ctx.fillStyle='#ffcd19';ctx.fillRect(0,0,512,128);
 ctx.fillStyle='#101820';ctx.font='900 90px Arial, sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('EDGE',256,66);
 const logo=new THREE.CanvasTexture(label);logo.colorSpace=THREE.SRGBColorSpace;
 for(const z of [-.39,.39]){const plaque=mesh(new THREE.PlaneGeometry(.8,.2),new THREE.MeshBasicMaterial({map:logo}),robot,-.08,1.21,z);if(z<0)plaque.rotation.y=Math.PI;}
 const legs=[];
 for(const x of [-.59,.59]) for(const z of [-.43,.43]){
  const hip=new THREE.Group();hip.position.set(x,1.18,z);robot.add(hip);ball(hip,.145,dark);const thigh=new THREE.Group();hip.add(thigh);rod(thigh,[0,0,0],[.2,-.48,0],.084,shell);ball(thigh,.106,dark,.2,-.48,0);const shin=new THREE.Group();shin.position.set(.2,-.48,0);thigh.add(shin);rod(shin,[0,0,0],[-.27,-.5,0],.052,dark);rod(shin,[.02,-.05,.03],[-.22,-.41,.03],.022,orange);box(shin,.24,.12,.17,black,-.27,-.52,0);legs.push({thigh,shin,phase:(x*z>0)?0:Math.PI});
 }
 const towers={};const names=['A','B','C','D'];names.forEach((id,i)=>{const g=new THREE.Group();g.position.set(-7+i*4.7,0,-3.2);site.add(g);box(g,.75,2.0,.75,dark,0,1,0);for(let y=.25;y<2;y+=.3)box(g,.78,.07,.78,mat(0x454541),0,y,0);const led=box(g,.8,.14,.8,glow(0x43586d),0,1.85,0);
 const canvas=document.createElement('canvas');canvas.width=512;canvas.height=128;const c=canvas.getContext('2d');c.fillStyle='#eef4ff';c.font='bold 56px Arial';c.textAlign='center';c.fillText('EDGE '+id,256,55);c.fillStyle='#9baec2';c.font='28px Arial';c.fillText(['Indoor lab','Private 5G yard','Remote perimeter','Wired dock'][i],256,98);const texture=new THREE.CanvasTexture(canvas);const label=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,transparent:true,depthTest:false}));label.position.y=2.6;label.scale.set(2.6,.65,1);g.add(label);towers[id]={g,led};});

 const linkMat=new THREE.LineBasicMaterial({color:0x66e6d3});const link=new THREE.Line(new THREE.BufferGeometry(),linkMat);site.add(link);
 const candidate=new THREE.Line(new THREE.BufferGeometry(),new THREE.LineDashedMaterial({color:0xffbd60,dashSize:.12,gapSize:.09}));site.add(candidate);
 function curve(line,from,to){const mid=from.clone().lerp(to,.5);mid.y=2.6;const points=new THREE.QuadraticBezierCurve3(from,mid,to).getPoints(40);line.geometry.dispose();line.geometry=new THREE.BufferGeometry().setFromPoints(points);line.computeLineDistances();}
 drag(rob.c,d=>site.rotation.y+=d);
 let lastState='',lastTime=0;
 function draw(now){requestAnimationFrame(draw);if(document.hidden)return;const dt=Math.min(.05,(now-lastTime)/1000);lastTime=now;
  if(!reduced)globe.rotation.y+=dt*.025;
  const obs=state.obs;robot.position.x=obs?-7+obs.position*14:-7;
  rob.camera.position.set(robot.position.x-5.3,3.5,6.2);rob.camera.lookAt(robot.position.x+2,1,0);
  ring.position.x=robot.position.x;
  const gait=state.playing&&!reduced?state.time*7:0;
  robot.position.y=state.playing&&!reduced?Math.sin(gait*2)*.025:0;
  legs.forEach(l=>{l.thigh.rotation.z=state.playing&&!reduced?Math.sin(gait+l.phase)*.25:0;l.shin.rotation.z=state.playing&&!reduced?Math.max(0,Math.sin(gait+l.phase))*.32:0;});
  const key=JSON.stringify([obs?.current_server,obs?.current_network,obs?.position,obs?.networks?.[obs?.current_network]?.available,state.handoff?.target,state.handoff?.phase]);
  if(key!==lastState){lastState=key;const active=obs?.current_server;const up=obs?.networks?.[obs?.current_network]?.available!==false;Object.entries(towers).forEach(([id,t])=>t.led.material.color.setHex(id===active&&up?(colors[obs.current_network]||0x66e6d3):0x43586d));link.visible=!!active&&!!towers[active]&&up;if(link.visible){link.material.color.setHex(colors[obs.current_network]||0x66e6d3);curve(link,new THREE.Vector3(robot.position.x,1.8,0),towers[active].g.position.clone().add(new THREE.Vector3(0,2,0)));}const h=state.handoff;candidate.visible=!!h?.target&&!!towers[h.target]&&['selected','prepare','transfer','check','switch'].includes(h.phase);if(candidate.visible)curve(candidate,new THREE.Vector3(robot.position.x,1.8,0),towers[h.target].g.position.clone().add(new THREE.Vector3(0,2,0)));}
  earth.renderer.render(earth.scene,earth.camera);mini.renderer.render(earth.scene,mini.camera);rob.renderer.render(rob.scene,rob.camera);
 }
 requestAnimationFrame(draw);
}catch(error){console.error('Mission visual unavailable',error);document.getElementById('visualFallback').hidden=false;}
