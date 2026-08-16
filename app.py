import qrcode

# Deine weltweite Web-Adresse von PythonAnywhere:
WEB_URL = "https://str222.pythonanywhere.com"

# QR-Code generieren
qr = qrcode.QRCode(box_size=10, border=4)
qr.add_data(WEB_URL)
qr.make(fit=True)

# Bild erzeugen und speichern
img = qr.make_image(fill_color="black", back_color="white")
img.save("shelly_qr.png")

print("✅ Neuer QR-Code wurde erfolgreich als 'shelly_qr.png' gespeichert!")