"""Write the Windows .ico (16 to 256 px) from the official DLNE icon.  python make_icon.py <icon-512.png> <out.ico>"""

import sys

from PIL import Image

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

if __name__ == "__main__":
    source, target = sys.argv[1], sys.argv[2]
    Image.open(source).convert("RGBA").save(target, format="ICO", sizes=SIZES)
    print(f"wrote {target}")
