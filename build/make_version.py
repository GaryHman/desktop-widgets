"""Write a PyInstaller version-info file (shown in Explorer > Properties > Details and used by Windows
as the app's display name)."""
import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True)       # product / file description, e.g. "KeepNotes Widgets"
ap.add_argument("--exe", required=True)        # internal name, e.g. "KeepNotesWidgets"
ap.add_argument("--ver", required=True)        # "3.0.0"
ap.add_argument("--company", default="Angels Inc.")
ap.add_argument("--out", required=True)
a = ap.parse_args()
parts = [int(x) for x in a.ver.split(".")] + [0] * 4
t = tuple(parts[:4])
v4 = ".".join(map(str, t))
text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{a.company}'),
      StringStruct('FileDescription', '{a.name}'),
      StringStruct('FileVersion', '{v4}'),
      StringStruct('InternalName', '{a.exe}'),
      StringStruct('LegalCopyright', '\\xa9 2026 {a.company} All rights reserved.'),
      StringStruct('OriginalFilename', '{a.exe}.exe'),
      StringStruct('ProductName', '{a.name}'),
      StringStruct('ProductVersion', '{a.ver}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
with open(a.out, "w", encoding="utf-8") as f:
    f.write(text)
print("wrote", a.out)
