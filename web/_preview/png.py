"""极简 PNG 读写：解码 + 裁剪 + 取色（无第三方依赖）。"""
import zlib, struct, sys


def read_png(path):
    data = open(path, 'rb').read()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, idat, w, h, bd, ct = 8, b'', None, None, None, None
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b'IHDR':
            w, h, bd, ct, _, _, _ = struct.unpack('>IIBBBBB', chunk)
        elif typ == b'IDAT':
            idat += chunk
        elif typ == b'IEND':
            break
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    bpp = ch * (bd // 8)
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ct, bd, bytes(out), stride


def write_png(path, w, h, rows, ct=6, bd=8):
    raw = b''.join(b'\x00' + r for r in rows)

    def chunk(typ, payload):
        return (struct.pack('>I', len(payload)) + typ + payload +
                struct.pack('>I', zlib.crc32(typ + payload) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', w, h, bd, ct, 0, 0, 0)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
                           chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def pixel(buf, stride, w, ct, x, y):
    ch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ct, 4)
    i = y * stride + x * ch
    if ch >= 3:
        return '#%02x%02x%02x' % (buf[i], buf[i + 1], buf[i + 2])
    return '#%02x%02x%02x' % (buf[i], buf[i], buf[i])


if __name__ == '__main__':
    path = sys.argv[1]
    w, h, ct, bd, buf, stride = read_png(path)
    print('size', w, h, 'ct', ct)
    for pair in sys.argv[2:]:
        x, y = (int(v) for v in pair.split(','))
        print(f'({x},{y}) {pixel(buf, stride, w, ct, x, y)}')
