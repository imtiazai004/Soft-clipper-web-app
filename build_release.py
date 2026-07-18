"""One-command release build for Soft Clipper.

Steps:
  1. Rebuild the React frontend (npm run build)
  2. (optional) Obfuscate core/ backend/ launcher.py with PyArmor -> build_obf/
  3. Bundle everything into a Windows folder app with PyInstaller
  4. Copy the friend-facing readme into the folder
  5. Zip the folder -> release/Soft-Clipper.zip

Obfuscation is OFF by default (the app is fully free without it). Turn it on
only if you have a PyArmor license:
    set OBFUSCATE=1   (Windows)  then run build_release.py

Run:  .venv\\Scripts\\python.exe build_release.py

Used both locally and by the GitHub Actions release workflow.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
APP_NAME = "Soft Clipper"
ZIP_NAME = "Soft-Clipper"
OBFUSCATE = os.environ.get("OBFUSCATE", "").strip() not in ("", "0", "false", "False")


def run(cmd, cwd=None):
    print(f"\n>>> {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd or ROOT, check=True)


def main():
    # 1. frontend
    npm = shutil.which("npm") or "npm"
    run([npm, "run", "build"], cwd=os.path.join(ROOT, "frontend"))

    # 2. obfuscate (optional) + choose spec
    if OBFUSCATE:
        print("\n[obfuscation ON — requires a PyArmor license for distribution]")
        shutil.rmtree(os.path.join(ROOT, "build_obf"), ignore_errors=True)
        run([PY, "-m", "pyarmor.cli", "gen", "-O", "build_obf", "-r",
             "core", "backend", "launcher.py"])
        spec = "build_obf.spec"
    else:
        print("\n[obfuscation OFF — free build]")
        spec = "build.spec"

    # 3. bundle
    shutil.rmtree(os.path.join(ROOT, "dist", APP_NAME), ignore_errors=True)
    run([PY, "-m", "PyInstaller", spec, "--noconfirm", "--clean"])

    dist_dir = os.path.join(ROOT, "dist", APP_NAME)
    if not os.path.isfile(os.path.join(dist_dir, f"{APP_NAME}.exe")):
        raise SystemExit("Build failed: exe not found")

    # 4. readme
    readme_src = os.path.join(ROOT, "dist_readme.txt")
    if os.path.isfile(readme_src):
        shutil.copy(readme_src, os.path.join(dist_dir, "READ ME FIRST.txt"))

    # 5. zip
    release_dir = os.path.join(ROOT, "release")
    os.makedirs(release_dir, exist_ok=True)
    zip_base = os.path.join(release_dir, ZIP_NAME)
    if os.path.isfile(zip_base + ".zip"):
        os.remove(zip_base + ".zip")
    print("\n>>> zipping...")
    shutil.make_archive(zip_base, "zip", root_dir=os.path.join(ROOT, "dist"), base_dir=APP_NAME)

    size_mb = os.path.getsize(zip_base + ".zip") / 1e6
    print(f"\nDONE -> {zip_base}.zip  ({size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
