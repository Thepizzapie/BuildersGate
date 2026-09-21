"""Blender-native low-poly aircraft. Metres; Blender -Y nose exports to Godot +Z."""
import bpy, math, json
from pathlib import Path
from mathutils import Vector

OUT = Path(__file__).resolve().parent
bpy.ops.wm.read_factory_settings(use_empty=True)

def material(name, rgb, rough=.45, metal=0):
    m=bpy.data.materials.new(name); m.diffuse_color=(*rgb,1); m.use_nodes=True
    p=m.node_tree.nodes.get('Principled BSDF')
    p.inputs['Base Color'].default_value=(*rgb,1)
    p.inputs['Roughness'].default_value=rough; p.inputs['Metallic'].default_value=metal
    return m

cream=material('Ivory airframe',(.86,.81,.66))
orange=material('Rescue orange',(.75,.10,.018))
teal=material('Petrol blue trim',(.045,.16,.19))
glass=material('Opaque blue glazing',(.065,.21,.29),.2,.25)
rubber=material('Rubber and propeller',(.024,.030,.035),.7)
metal=material('Brushed aluminium',(.42,.47,.49),.32,.65)
red=material('Port navigation light',(.85,.025,.018),.25)
green=material('Starboard navigation light',(.025,.65,.25),.25)
mats=[cream,orange,teal,glass,rubber,metal,red,green]

def empty(name, loc=(0,0,0), parent=None):
    ob=bpy.data.objects.new(name,None); bpy.context.collection.objects.link(ob)
    ob.location=loc; ob.parent=parent; ob.empty_display_size=.25
    return ob

root=empty('MeridianScout')
root['forward_axis']='Godot +Z'; root['units']='metres'
body=empty('Airframe',parent=root)

def mesh(name, vertices, faces, ids=None, parent=body):
    data=bpy.data.meshes.new(name); data.from_pydata(vertices,[],faces); data.update()
    ob=bpy.data.objects.new(name,data); bpy.context.collection.objects.link(ob); ob.parent=parent
    for m in mats: data.materials.append(m)
    for i,p in enumerate(data.polygons): p.material_index=ids[i] if ids else 0
    # Recalculate outward normals using Blender's mesh operator.
    bpy.context.view_layer.objects.active=ob; ob.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False); bpy.ops.object.mode_set(mode='OBJECT')
    ob.select_set(False)
    return ob

def box(name, loc, size, mi, parent=body, bevel=0):
    bpy.ops.mesh.primitive_cube_add(size=1,location=loc); ob=bpy.context.object; ob.name=name
    ob.dimensions=size; bpy.ops.object.transform_apply(location=False,rotation=False,scale=True)
    ob.data.materials.append(mats[mi]); ob.parent=parent
    if bevel:
        mod=ob.modifiers.new('Small corner facets','BEVEL'); mod.width=bevel; mod.segments=1
        bpy.ops.object.modifier_apply(modifier=mod.name)
    ob.select_set(False); return ob

def rod(name, a, b, radius, mi, parent=body, sides=8):
    a,b=Vector(a),Vector(b); d=b-a
    bpy.ops.mesh.primitive_cylinder_add(vertices=sides,radius=radius,depth=d.length,location=(a+b)/2)
    ob=bpy.context.object; ob.name=name; ob.rotation_euler=d.to_track_quat('Z','Y').to_euler()
    ob.data.materials.append(mats[mi]); ob.parent=parent; ob.select_set(False); return ob

# Continuous eight-sided fuselage, with an integrated blue lower belt.
stations=[(-2.68,1.50,.30,.31),(-2.24,1.49,.44,.40),(-1.38,1.46,.55,.46),
          (-.35,1.44,.60,.47),(.64,1.43,.51,.43),(1.60,1.25,.30,.29),
          (2.62,.95,.145,.16),(3.14,.90,.07,.085)]
verts=[]
for y,z,rx,rz in stations:
    for j in range(8):
        a=math.tau*j/8+math.pi/8; verts.append((rx*math.cos(a),y,z+rz*math.sin(a)))
faces=[tuple(reversed(range(8)))]; ids=[1]
for k in range(len(stations)-1):
    for j in range(8):
        faces.append((k*8+j,k*8+(j+1)%8,(k+1)*8+(j+1)%8,(k+1)*8+j))
        ids.append(1 if k==0 else (2 if j in (4,7) else 0))
faces.append(tuple(range((len(stations)-1)*8,len(stations)*8))); ids.append(0)
mesh('ContinuousFuselage',verts,faces,ids)

# Cowling grille and small exhaust, physically attached to the nose.
box('NoseIntake',(0,-2.695,1.40),(.33,.025,.10),4,bevel=.018)
rod('Exhaust',(.30,-1.98,1.16),(.38,-2.07,.99),.055,5)

# Faceted canopy with broad windscreen, side panes and structural pillars.
canopy=[(-.50,-1.32,1.68),(.50,-1.32,1.68),(-.43,-.90,2.30),(.43,-.90,2.30),
        (-.49,.51,2.30),(.49,.51,2.30),(-.46,.91,1.74),(.46,.91,1.74)]
mesh('CockpitGlazing',canopy,[(0,1,3,2),(0,2,4,6),(1,7,5,3),(2,3,5,4),(6,4,5,7)], [3,3,3,0,3])
for a,b in [(0,2),(1,3),(2,4),(3,5),(4,6),(5,7),(0,1),(2,3),(4,5),(6,7)]:
    rod('CanopyFrame',canopy[a],canopy[b],.027,0)
for s in [-1,1]:
    rod('DoorPillar',(s*.50,-.06,1.74),(s*.47,-.06,2.30),.025,0)
    box('DoorHandle',(s*.514,.24,1.70),(.028,.16,.025),5)
    rod('BoardingStep',(s*.30,.35,1.15),(s*.86,.35,.80),.024,5)
    rod('BoardingStepTread',(s*.86,.18,.80),(s*.86,.55,.80),.026,4)

# Closed airfoil shells. Ailerons and flaps hinge on a separate trailing edge.
def wing_half(s):
    spans=[(.0,-.84,.78,2.33),(.70,-.84,.78,2.33),(3.85,-.73,.78,2.41),(4.57,-.51,.78,2.45),(4.72,-.34,.46,2.45)]
    vs=[]
    for x,front,back,z in spans:
        vs.extend([(s*x,front,z),(s*x,front+.22,z+.11),(s*x,back-.12,z+.065),
                   (s*x,back,z),(s*x,back-.14,z-.045),(s*x,front+.20,z-.055)])
    fs=[tuple(reversed(range(6)))]; mi=[0]
    for k in range(len(spans)-1):
        for j in range(6):
            fs.append((k*6+j,k*6+(j+1)%6,(k+1)*6+(j+1)%6,(k+1)*6+j))
            mi.append(1 if k>=2 else 0)
    fs.append(tuple(range((len(spans)-1)*6,len(spans)*6))); mi.append(1)
    mesh('WingLeft' if s<0 else 'WingRight',vs,fs,mi)
    rod('WingLiftStrut',(s*.40,.22,1.22),(s*2.82,.29,2.39),.037,2)
    rod('WingDragStrut',(s*.39,.55,1.24),(s*2.82,.29,2.39),.025,2)
    for label,inner,outer in [('Flap',.72,2.28),('Aileron',2.32,4.35)]:
        hinge_z=2.33+max(0,inner-.7)*.08/3.15
        pivot=empty(label+('Left' if s<0 else 'Right'),(s*inner,.775,hinge_z),root)
        length=outer-inner
        rise=length*.08/3.15
        vs=[(0,0,0),(s*length,0,rise),(s*length,.27,rise-.025),(0,.30,-.025),
            (0,0,-.04),(s*length,0,rise-.04),(s*length,.27,rise-.060),(0,.30,-.065)]
        mesh(label+'Surface',vs,[(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)], [0]*6,pivot)
    box('WingtipLight',(s*4.61,.02,2.46),(.10,.15,.08),6 if s<0 else 7,bevel=.025)
wing_half(-1); wing_half(1)

# Tailplane with distinct elevator pivots.
tail_start=set(bpy.data.objects)
for s in [-1,1]:
    vs=[(0,2.12,1.51),(s*1.48,2.43,1.56),(s*1.60,2.73,1.56),(0,2.73,1.51)]
    vs+= [(x,y,z-.075) for x,y,z in vs]
    mesh('Tailplane',vs,[(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)],[0,0,0,1,0,0])
    pivot=empty('ElevatorLeft' if s<0 else 'ElevatorRight',(0,2.74,1.51),root)
    vs=[(0,0,0),(s*1.59,0,.05),(s*1.46,.33,.025),(0,.37,-.025)]
    vs += [(x,y,z-.045) for x,y,z in vs]
    mesh('ElevatorSurface',vs,[(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)],[1]*6,pivot)

def fin(name, outline, half, mi, parent=body):
    vs=[(x,y,z) for x in [-half,half] for y,z in outline]; n=len(outline)
    fs=[tuple(reversed(range(n))),tuple(range(n,2*n))]
    for j in range(n): fs.append((j,(j+1)%n,(j+1)%n+n,j+n))
    return mesh(name,vs,fs,[mi]*len(fs),parent)
fin('VerticalFin',[(1.98,1.48),(2.50,2.79),(2.79,2.81),(2.82,1.46)],.065,1)
rudder=empty('Rudder',(0,2.82,1.48),root)
fin('RudderSurface',[(0,0),(-.025,1.33),(.24,1.18),(.37,.07)],.055,0,rudder)
# Flush inset accent on each fin side.
for s in [-1,1]:
    mesh('TailStripe',[(s*.066,2.32,2.25),(s*.066,2.80,2.25),(s*.066,2.80,2.38),(s*.066,2.37,2.38)],[(0,1,2,3)],[2])
for ob in set(bpy.data.objects)-tail_start:
    if ob.parent in (root,body):ob.location.z-=.5

# Fixed taildragger gear. Each wheel keeps an axle-centred transform.
def wheel(name, center, radius, width):
    pivot=empty(name,center,root)
    profile=[(-width*.5,0),(-width*.5,radius*.48),(-width*.5,radius*.74),(-width*.34,radius),
             (width*.34,radius),(width*.5,radius*.74),(width*.5,radius*.48),(width*.5,0)]
    vs=[]
    for x,r in profile:
        for j in range(12):
            a=math.tau*j/12; vs.append((x,math.cos(a)*r,math.sin(a)*r))
    fs=[]; mi=[]
    for k in range(len(profile)-1):
        for j in range(12):
            fs.append((k*12+j,k*12+(j+1)%12,(k+1)*12+(j+1)%12,(k+1)*12+j)); mi.append(5 if k in (0,6) else 4)
    mesh(name+'Mesh',vs,fs,mi,pivot)
for s in [-1,1]:
    rod('MainGearLeg',(s*.42,-.73,1.11),(s*1.02,-.96,.38),.057,5)
    rod('GearBrace',(s*.32,.18,1.13),(s*1.02,-.96,.38),.035,2)
    wheel('WheelLeft' if s<0 else 'WheelRight',(s*1.04,-.96,.38),.38,.26)
rod('TailGearSpring',(0,2.37,.91),(0,2.74,.22),.030,5)
wheel('TailWheel',(0,2.78,.19),.19,.14)

# Separate propeller assembly with its origin on the crankshaft.
prop=empty('Propeller',(0,-2.79,1.50),root)
rod('PropellerHub',(0,-.075,0),(0,.04,0),.13,5,prop,12)
for sign in [-1,1]:
    outline=[(-.075,sign*.10),(-.12,sign*.46),(-.065,sign*1.04),(.07,sign*1.08),(.15,sign*.40),(.075,sign*.10)]
    vs=[(x,y,z) for y in [-.035,.035] for x,z in outline]; n=len(outline)
    fs=[tuple(reversed(range(n))),tuple(range(n,2*n))]
    fs += [(j,(j+1)%n,(j+1)%n+n,j+n) for j in range(n)]
    mesh('PropellerBlade',vs,fs,[4]*len(fs),prop)
    box('PropellerTip',(.015,0,sign*.98),(.15,.078,.15),1,prop,bevel=.015)
bpy.ops.mesh.primitive_cone_add(vertices=12,radius1=.16,radius2=.018,depth=.26,location=(0,-.20,0))
spinner=bpy.context.object; spinner.name='Spinner'; spinner.rotation_euler.x=math.pi/2
spinner.data.materials.append(orange); spinner.parent=prop; spinner.select_set(False)

for name,loc in [('PilotSeat',(0,-.20,1.48)),('CockpitCamera',(0,-.35,2.03)),
                 ('EntryLeft',(-1.45,-.10,.15)),('EntryRight',(1.45,-.10,.15)),
                 ('CenterOfMass',(0,-.10,1.40)),('ChaseCamera',(0,8,4))]:
    empty(name,loc,root)

# Batch static surfaces and each moving assembly; preserve meaningful pivots.
def join_children(parent, name):
    obs=[o for o in parent.children if o.type=='MESH']
    if not obs:return
    bpy.ops.object.select_all(action='DESELECT')
    for ob in obs:ob.select_set(True)
    bpy.context.view_layer.objects.active=obs[0]; bpy.ops.object.join()
    ob=bpy.context.object; ob.name=name
    bpy.ops.object.transform_apply(location=False,rotation=True,scale=True)
    bpy.context.scene.cursor.location=parent.matrix_world.translation
    bpy.ops.object.origin_set(type='ORIGIN_CURSOR'); ob.select_set(False)
join_children(body,'AirframeMesh')
for ob in list(root.children):
    if ob!=body and ob.type=='EMPTY':join_children(ob,ob.name+'Mesh')
bpy.context.view_layer.update()

asset=[root]+list(root.children_recursive)
triangles=0
for ob in asset:
    if ob.type=='MESH':ob.data.calc_loop_triangles(); triangles+=len(ob.data.loop_triangles)
bpy.ops.object.select_all(action='DESELECT')
for ob in asset:ob.select_set(True)
bpy.context.view_layer.objects.active=root
bpy.ops.export_scene.gltf(filepath=str(OUT/'meridian_scout.glb'),export_format='GLB',use_selection=True,
                          export_yup=True,export_extras=True,export_animations=False)
positions=[ob.matrix_world@v.co for ob in asset if ob.type=='MESH' for v in ob.data.vertices]
dimensions=[max(p[i] for p in positions)-min(p[i] for p in positions) for i in range(3)]
metrics={'triangles':triangles,'mesh_nodes':sum(o.type=='MESH' for o in asset),
         'materials':len(mats),'wingspan_m':round(dimensions[0],3),'length_m':round(dimensions[1],3),
         'height_m':round(dimensions[2],3),'forward':'Godot +Z',
         'file_bytes':(OUT/'meridian_scout.glb').stat().st_size}
(OUT/'metrics.json').write_text(json.dumps(metrics,indent=2))
print('SCOUT_ASSET',json.dumps(metrics))

# Source file includes a separate render-only studio collection.
bpy.ops.object.select_all(action='DESELECT')
studio=bpy.data.collections.new('Studio - not exported'); bpy.context.scene.collection.children.link(studio)
def to_studio(ob):
    for col in list(ob.users_collection):col.objects.unlink(ob)
    studio.objects.link(ob)
floor=box('StudioFloor',(0,0,-.11),(200,200,.16),0,parent=None)
floor.data.materials.clear(); floor.data.materials.append(material('Studio slate',(.11,.16,.19),.85)); to_studio(floor)
scene=bpy.context.scene; scene.world=bpy.data.worlds.new('StudioWorld'); scene.world.use_nodes=True
scene.world.node_tree.nodes['Background'].inputs[0].default_value=(.22,.29,.35,1)
scene.world.node_tree.nodes['Background'].inputs[1].default_value=.45
for name,loc,power,size in [('Key',(2,-7,10),1900,7),('Fill',(-7,-1,6),1400,6),('Rim',(3,7,8),2200,5)]:
    data=bpy.data.lights.new(name,'AREA'); ob=bpy.data.objects.new(name,data); studio.objects.link(ob)
    ob.location=loc; ob.rotation_euler=(Vector((0,0,1.3))-ob.location).to_track_quat('-Z','Y').to_euler()
    data.energy=power; data.shape='DISK'; data.size=size
data=bpy.data.cameras.new('PreviewCamera'); camera=bpy.data.objects.new('PreviewCamera',data); studio.objects.link(camera)
scene.camera=camera; data.type='ORTHO'; data.ortho_scale=12.1
scene.render.engine='CYCLES'; scene.cycles.samples=48
scene.render.resolution_x=1400; scene.render.resolution_y=1050; scene.render.resolution_percentage=100
scene.view_settings.view_transform='AgX'
for name,loc in [('preview',(10,-14,8)),('rear',(-10,13,7))]:
    camera.location=loc; camera.rotation_euler=(Vector((0,0,1.3))-camera.location).to_track_quat('-Z','Y').to_euler()
    scene.render.filepath=str(OUT/(name+'.png')); bpy.ops.render.render(write_still=True)
camera.location=(10,-14,8); camera.rotation_euler=(Vector((0,0,1.3))-camera.location).to_track_quat('-Z','Y').to_euler()
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'meridian_scout.blend'))
