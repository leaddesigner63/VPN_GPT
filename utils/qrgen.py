import qrcode
from io import BytesIO

def make_qr(link: str):
    img = qrcode.make(link)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

