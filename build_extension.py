"""Build spriteloom-<version>.zip and hot-deploy to every Blender extension directory found.

Deploys to *all* installs it can see, not just the host OS's. Run from WSL and both the
Linux and the Windows install are updated; that is deliberate. The two copies silently
drifting apart -- because this script only ever knew about %APPDATA% -- is what left the
WSL install five weeks behind and broke the equine geo scripts, which call an API the old
build did not have.
"""
import zipfile
import os
import shutil
import glob
import re

files = [
    "blender_manifest.toml",
    "__init__.py",
    "spriteloom_addon.py",
    "spriteloom_render.py",
]

# Read version from manifest
version = "0.0.0"
with open("blender_manifest.toml") as f:
    for line in f:
        m = re.match(r'^version\s*=\s*"([^"]+)"', line)
        if m:
            version = m.group(1)
            break

# Build zip
output = f"spriteloom-{version}.zip"
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
    for f in files:
        zf.write(f)
print(f"Built {output}")

# Every place a Blender extension can live, across the OSes this project is driven from.
# The trailing component is always <blender version>/extensions/user_default/spriteloom.
_TAIL = os.path.join("extensions", "user_default", "spriteloom")
patterns = [
    # Windows, run natively
    os.path.join(os.path.expandvars(r"%APPDATA%"), "Blender Foundation", "Blender", "*", _TAIL),
    # Linux / WSL, run natively
    os.path.join(os.path.expanduser("~"), ".config", "blender", "*", _TAIL),
    # macOS, run natively
    os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Blender", "*", _TAIL),
    # Windows installs seen from inside WSL
    os.path.join("/mnt", "*", "Users", "*", "AppData", "Roaming", "Blender Foundation", "Blender", "*", _TAIL),
]

seen = set()
install_dirs = []
for pattern in patterns:
    if "%" in pattern:          # unexpanded %APPDATA% -- not on Windows
        continue
    for d in sorted(glob.glob(pattern)):
        key = os.path.realpath(d)
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            install_dirs.append(d)

if not install_dirs:
    print("No installed spriteloom extension found -- install from zip first")
else:
    for install_dir in install_dirs:
        for f in files:
            shutil.copy2(f, os.path.join(install_dir, f))
        # A stale __pycache__ next to a replaced source file is a silent way to keep
        # running the old module, so clear it rather than trusting mtime invalidation.
        cache = os.path.join(install_dir, "__pycache__")
        if os.path.isdir(cache):
            shutil.rmtree(cache, ignore_errors=True)
        print(f"Deployed to {install_dir}")
    print(f"Deployed to {len(install_dirs)} install(s). Reload in Blender: F3 > 'Reload Scripts'")
