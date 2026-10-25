"""Ícono "DSH" de la bandeja: las letras cambian de color según el estado del puente.

Fondo oscuro redondeado, para que se distinga en barras de tareas claras y oscuras. Tipografía condensada
(Bahnschrift Bold Condensed, incluida en Windows 10/11) para que las tres letras se lean a 16 px.
"""
from PIL import Image, ImageDraw, ImageFont

# Estado del servicio → color de las letras
COLORS = {
    "listo": "#3B82F6",  # azul: control conectado, listo para jugar
    "en_juego": "#22C55E",  # verde: un juego está usando el control
    "esperando_control": "#F59E0B",  # ámbar: enciende el control
    "cable": "#F59E0B",  # ámbar: el juego está usando el control por cable
    "iniciando": "#94A3B8",  # gris
    "detenido": "#64748B",  # gris oscuro: en pausa
    "falta_configurar": "#EF4444",  # rojo: hay que instalar componentes
    "problema": "#EF4444",  # rojo
}
BACKGROUND = "#0B1220"
BRAND = COLORS["listo"]


LETTERS = "DSH"


def _font(size):
    """Bahnschrift Bold Condensed; si no está (Windows muy viejo), Segoe UI Bold algo más chica."""
    try:
        font = ImageFont.truetype("bahnschrift.ttf", size)
        font.set_variation_by_name("Bold Condensed")
        return font
    except (OSError, ValueError):
        pass
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, int(size * 0.66))
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(color=BRAND, size=64):
    """Imagen RGBA cuadrada con "DSH" en el color dado. Se dibuja a 4× y se reduce, para bordes suaves."""
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=BACKGROUND)
    font = _font(int(big * 0.62))
    box = draw.textbbox((0, 0), LETTERS, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    if w > big * 0.9:  # que nunca se salga del cuadro
        font = _font(int(big * 0.62 * big * 0.9 / w))
        box = draw.textbbox((0, 0), LETTERS, font=font)
        w, h = box[2] - box[0], box[3] - box[1]
    draw.text(((big - w) / 2 - box[0], (big - h) / 2 - box[1]), LETTERS, font=font, fill=color)
    return img.resize((size, size), Image.LANCZOS)


def icon_for_state(state):
    return make_icon(COLORS.get(state, COLORS["problema"]))


def save_ico(path):
    """Ícono de la aplicación (.ico con varios tamaños) para el .exe."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    make_icon(BRAND, 256).save(path, sizes=[(s, s) for s in sizes])
