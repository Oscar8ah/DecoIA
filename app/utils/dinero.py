"""
Cuentas de dinero de DecoIArte — UN solo lugar para todas.

Reglas (no se tocan sin pensarlo dos veces):
  · Todo en PESOS ENTEROS (int). El peso colombiano no se usa con centavos;
    Wompi cobra en centavos, así que solo al hablar con Wompi se multiplica ×100.
  · Redondeo "mitad hacia arriba" (el de toda la vida), nunca el del banquero.
  · La comisión de DecoIArte se cobra SOLO sobre los productos. El domicilio
    es 100 % de la tienda (ella lo pone y ella lo paga).
  · El comprador paga el PRECIO PUBLICADO + domicilio: nada más, nada menos.
  · A la tienda se le descuentan los COSTOS ADMINISTRATIVOS, que se le muestran
    como UNA sola línea: comisión de DecoIArte + costo de la pasarela Wompi
    (2,65 % + $700 + IVA 19 % sobre el TOTAL). Decisión del 29 sep.
  · Lo de la tienda = total − costos administrativos. Siempre se cumple:
        comisión + costo_wompi + para_tienda == total   (se verifica en cada cálculo)
  · DecoIArte se queda con la comisión completa (Wompi sale de la parte de la tienda).
"""
from decimal import Decimal, ROUND_HALF_UP

WOMPI_MINIMO_COP = 1_500            # modelo Agregador: Wompi no cobra montos menores
DOMICILIO_MAXIMO_COP = 50_000_000   # tope de cordura para un domicilio (evita un cero de más)
WOMPI_PORCENTAJE = Decimal("0.0265")
WOMPI_FIJO_COP = 700
IVA = Decimal("0.19")


def pesos(valor) -> int:
    """Convierte a pesos enteros con redondeo mitad hacia arriba."""
    return int(Decimal(str(valor or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def comision_de(subtotal_productos: int, porcentaje) -> int:
    return pesos(Decimal(subtotal_productos) * Decimal(str(porcentaje)) / Decimal(100))


def costo_wompi_estimado(total: int) -> int:
    base = Decimal(total) * WOMPI_PORCENTAJE + WOMPI_FIJO_COP
    return pesos(base * (1 + IVA))


def repartir(subtotal_productos: int, domicilio: int, porcentaje) -> dict:
    """Reparto completo de un pedido. Lanza ValueError si algo no cuadra."""
    subtotal_productos, domicilio = int(subtotal_productos), int(domicilio)
    if subtotal_productos < 0 or domicilio < 0:
        raise ValueError("Montos negativos")
    comision = comision_de(subtotal_productos, porcentaje)
    total = subtotal_productos + domicilio
    wompi = costo_wompi_estimado(total)
    costos_administrativos = comision + wompi          # lo que se le descuenta a la tienda, en una sola línea
    para_tienda = total - costos_administrativos
    if comision + wompi + para_tienda != total:        # nunca debería pasar; si pasa, se detiene todo
        raise ValueError("El reparto no cuadra")
    return {"subtotal": subtotal_productos, "domicilio": domicilio, "total": total,
            "comision": comision, "costo_wompi": wompi, "costos_administrativos": costos_administrativos,
            "para_tienda": para_tienda, "neto_decoiarte": comision}


def en_letras(n: int) -> str:
    """Número en palabras (hasta miles de millones), para que nadie se equivoque de ceros."""
    n = int(n)
    if n == 0:
        return "cero"
    U = ["", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce", "trece",
         "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós",
         "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho", "veintinueve"]
    D = ["", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
    C = ["", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos", "setecientos",
         "ochocientos", "novecientos"]
    def hasta_999(x):
        if x == 100: return "cien"
        c, r = divmod(x, 100); partes = [C[c]] if c else []
        if r < 30: partes.append(U[r]) if r else None
        else:
            d, u = divmod(r, 10); partes.append(D[d] + (f" y {U[u]}" if u else ""))
        return " ".join(p for p in partes if p)
    def apocope(txt):   # "uno" → "un" / "veintiuno" → "veintiún" delante de mil y millones
        if txt.endswith("veintiuno"): return txt[:-9] + "veintiún"
        if txt.endswith("uno"): return txt[:-3] + "un"
        return txt
    partes = []
    millones, resto = divmod(n, 1_000_000)
    miles, unidades = divmod(resto, 1000)
    if millones:
        partes.append("un millón" if millones == 1 else f"{apocope(en_letras(millones))} millones")
    if miles:
        partes.append("mil" if miles == 1 else f"{apocope(hasta_999(miles))} mil")
    if unidades:
        partes.append(hasta_999(unidades))
    return " ".join(partes).replace("  ", " ")