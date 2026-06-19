import zipfile, os, re

p = "output/chicago_cli/full_chicago_cli_2layer_0619_0936.3mf"
with zipfile.ZipFile(p) as z:
    obj = z.read("3D/Objects/object_1.model").decode()
    objects = re.findall(r'<object id="(\d+)"', obj)
    print(f"Sub-meshes: {len(objects)} (ids: {', '.join(objects)})")
    if "6" in objects:
        print(f"Landmarks (id=6): PRESENT  E1 white")
    else:
        print(f"Landmarks (id=6): MISSING!")
    print(f"File size: {os.path.getsize(p)/1024/1024:.1f} MB")
