import qrcode, io

def make_qr(link: str):
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

