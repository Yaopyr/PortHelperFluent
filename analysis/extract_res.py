import pefile, os, struct, json, sys

SRC = r"C:\Users\Yaopyr\Desktop\PortHelper.exe"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parsed")
os.makedirs(OUT, exist_ok=True)
pe = pefile.PE(SRC)


def res_blob(tid, rid):
    for e in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if not e.name and e.id == tid:
            for e2 in e.directory.entries:
                if (not e2.name and e2.id == rid) or (e2.name and e2.name.string.decode("utf-16le", "replace") == str(rid)):
                    for e3 in e2.directory.entries:
                        d = e3.data.struct
                        return pe.get_data(d.OffsetToData, d.Size)
    return None


def read_sz(buf, off):
    end = off
    while end + 1 < len(buf) and buf[end:end + 2] != b"\x00\x00":
        end += 2
    return buf[off:end].decode("utf-16le", "replace"), end + 2


def read_sz_or_ord(buf, off):
    if off + 2 > len(buf):
        return "", off
    w = struct.unpack_from("<H", buf, off)[0]
    if w == 0:
        return "", off + 2
    if w == 0xFFFF:
        return "#%d" % struct.unpack_from("<H", buf, off + 2)[0], off + 4
    return read_sz(buf, off)


def parse_dialog(data):
    dlgver, sig = struct.unpack_from("<HH", data, 0)
    is_ex = dlgver == 1 and sig == 0xFFFF
    off = 0
    if is_ex:
        off = 4
        helpid, exstyle, style, cdit = struct.unpack_from("<IIII", data, off)
        off += 16
    else:
        style, exstyle = struct.unpack_from("<II", data, off)
        off += 8
        cdit = struct.unpack_from("<H", data, off)[0]
        off += 2
        helpid = 0
    x, y, cx, cy = struct.unpack_from("<hhhh", data, off)
    off += 8
    menu, off = read_sz_or_ord(data, off)
    cls, off = read_sz_or_ord(data, off)
    title, off = read_sz_or_ord(data, off)
    d = {
        "id": None, "ex": is_ex, "helpid": helpid, "style": "0x%08x" % style,
        "exstyle": "0x%08x" % exstyle, "rect": [x, y, cx, cy], "menu": menu,
        "class": cls, "title": title, "controls": [],
    }
    if is_ex:
        pts, wt, it, cs = struct.unpack_from("<HHBB", data, off)
        off += 6
        d.update(points=pts / 10, weight=wt, italic=it, charset=cs)
    else:
        pts = struct.unpack_from("<H", data, off)[0]
        off += 2
        d["points"] = pts / 10
    if style & 0x40:  # DS_SETFONT
        face, off = read_sz(data, off)
        d["font"] = face
    for _ in range(cdit):
        if is_ex:
            off = (off + 3) & ~3
            chid = struct.unpack_from("<I", data, off)[0]
            off += 4
        else:
            chid = None
        off = (off + 3) & ~3
        cst, cex, ccx, ccy, ccw, cch, cid = struct.unpack_from("<IIhhhhI", data, off)
        off += 20
        cl, off = read_sz_or_ord(data, off)
        txt, off = read_sz_or_ord(data, off)
        extra = struct.unpack_from("<H", data, off)[0]
        off += 2 + extra
        d["controls"].append({
            "helpid": chid, "id": cid, "class": cl, "text": txt,
            "rect": [ccx, ccy, ccw, cch], "style": "0x%08x" % cst,
        })
    return d


dialogs = []
for e in pe.DIRECTORY_ENTRY_RESOURCE.entries:
    if not e.name and e.id == 5:
        for e2 in e.directory.entries:
            data = res_blob(5, e2.id)
            try:
                d = parse_dialog(data)
                d["id"] = e2.id
                dialogs.append(d)
            except Exception as ex:
                dialogs.append({"id": e2.id, "error": repr(ex)})

json.dump(dialogs, open(os.path.join(OUT, "dialogs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
with open(os.path.join(OUT, "dialogs.txt"), "w", encoding="utf-8") as f:
    for d in dialogs:
        if "error" in d:
            f.write("\n=== DIALOG %s ERROR %s\n" % (d["id"], d["error"]))
            continue
        f.write("\n=== DIALOG %s '%s' rect=%s points=%s font=%s class=%s style=%s\n" % (
            d["id"], d["title"], d["rect"], d.get("points"), d.get("font"), d["class"], d["style"]))
        for c in d["controls"]:
            f.write("  id=%-5s %-20s rect=%-22s style=%-12s '%s'\n" % (
                c["id"], c["class"][:20], str(c["rect"]), c["style"], c["text"]))

strings = {}
for e in pe.DIRECTORY_ENTRY_RESOURCE.entries:
    if not e.name and e.id == 6:
        for e2 in e.directory.entries:
            data = res_blob(6, e2.id)
            off = 0
            for i in range(16):
                if off + 2 > len(data):
                    break
                ln = struct.unpack_from("<H", data, off)[0]
                off += 2
                s = data[off:off + ln * 2].decode("utf-16le", "replace")
                off += ln * 2
                if s:
                    strings[e2.id * 16 + i] = s
json.dump(strings, open(os.path.join(OUT, "strings.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
with open(os.path.join(OUT, "strings.txt"), "w", encoding="utf-8") as f:
    for k, v in sorted(strings.items()):
        f.write("%5d %r\n" % (k, v))

print("dialogs", len(dialogs), "strings", len(strings))
