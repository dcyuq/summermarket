import io
import re

import cv2
import numpy as np
import qrcode
from PIL import Image

AMOUNT = re.compile(r"^\d{1,10}(\.\d{1,2})?$")


class QRError(Exception):
    pass


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def parse(payload):
    raw = payload.encode("utf-8")
    fields = {}
    i = 0
    while i < len(raw):
        if i + 4 > len(raw):
            raise QRError("truncated payload")
        tag = raw[i:i + 2].decode("ascii", "replace")
        try:
            length = int(raw[i + 2:i + 4])
        except ValueError:
            raise QRError("bad length field")
        value = raw[i + 4:i + 4 + length]
        if len(value) != length:
            raise QRError("truncated value")
        fields[tag] = value.decode("utf-8", "replace")
        i += 4 + length
    return fields


def build(fields):
    out = b""
    for tag in sorted(fields):
        if tag == "63":
            continue
        value = fields[tag].encode("utf-8")
        out += tag.encode("ascii") + f"{len(value):02d}".encode("ascii") + value
    out += b"6304"
    return (out + crc16(out).encode("ascii")).decode("utf-8")


def validate(payload):
    if not payload or not payload.startswith("0002"):
        raise QRError("that does not look like a payment qr.")
    if len(payload) < 20:
        raise QRError("payload too short.")
    fields = parse(payload)
    body = payload[:-4].encode("utf-8")
    if crc16(body) != payload[-4:].upper():
        raise QRError("checksum failed, the qr may be damaged.")
    if fields.get("53") != "608" or fields.get("58") != "PH":
        raise QRError("that is not a philippine peso qr.")
    return fields


def set_amount(payload, amount):
    if not AMOUNT.match(amount):
        raise QRError("amount must look like 30 or 30.00")
    fields = parse(payload)
    fields["01"] = "12"
    fields["54"] = amount
    return build(fields)


def read_image(data):
    array = np.frombuffer(data, np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise QRError("could not open that image.")
    detector = cv2.QRCodeDetector()
    for candidate in (image, cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)):
        text, _, _ = detector.detectAndDecode(candidate)
        if text:
            return text
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, sharp = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text, _, _ = detector.detectAndDecode(sharp)
    if text:
        return text
    raise QRError("no qr code found in that image.")


def render(payload, size=600):
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(payload)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white").convert("RGB")
    image = image.resize((size, size), Image.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer