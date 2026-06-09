# ── AGREGAR AL FINAL DE imagen_service.py ────────────────────────────────

async def generar_imagen_con_producto(
    foto_bytes: bytes,
    producto_bytes: bytes,
    producto_nombre: str,
    categoria: str = "material",
) -> str:
    """
    Aplica un producto específico (piso, enchape, pintura) a la foto del espacio.
    Usa la imagen del producto como referencia visual para gpt-image-1.
    """
    settings = get_settings()

    # Mapear categoría a instrucción de prompt
    instrucciones = {
        "pisos":      f"Replace the floor with the exact flooring material shown in the reference image: {producto_nombre}. Keep all furniture and walls unchanged.",
        "enchapes":   f"Apply the tile/enchape material from the reference image ({producto_nombre}) to the walls and floor. Keep furniture unchanged.",
        "pintura":    f"Paint the walls with the exact color and finish shown in the reference image: {producto_nombre}. Keep all furniture and floor unchanged.",
        "materiales": f"Apply the material from the reference image ({producto_nombre}) to the floor. Keep everything else unchanged.",
    }
    instruccion = instrucciones.get(
        categoria,
        f"Apply the product from the reference image ({producto_nombre}) to the space. Keep furniture unchanged."
    )

    prompt = (
        f"Interior design renovation. {instruccion} "
        f"Photorealistic result. Professional architectural photography. "
        f"Same room layout, same furniture positions, same lighting angle. "
        f"No text, no watermarks."
    )

    imagen_png   = imagen_a_png_1024(foto_bytes)
    mascara_png  = crear_mascara_piso_paredes(foto_bytes)

    # Redimensionar producto a 1024x1024 también
    prod_img = Image.open(io.BytesIO(producto_bytes)).convert("RGBA")
    prod_img = prod_img.resize((1024, 1024), Image.LANCZOS)
    prod_buf = io.BytesIO()
    prod_img.save(prod_buf, format="PNG")
    producto_png = prod_buf.getvalue()

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={
                "image": ("room.png",    imagen_png,   "image/png"),
                "mask":  ("mask.png",    mascara_png,  "image/png"),
            },
            data={
                "model":   "gpt-image-1",
                "prompt":  prompt,
                "n":       "1",
                "size":    "1024x1024",
                "quality": "high",
            }
        )

        logger.info(f"generar_imagen_con_producto status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                resultado_bytes = base64.b64decode(imagen_b64)
                url = await subir_imagen_a_imgbb(resultado_bytes, settings.imgbb_api_key)
                return url
            else:
                return data["data"][0].get("url")
        else:
            logger.error(f"Error generar_imagen_con_producto: {response.text[:300]}")
            # Fallback: devolver la foto original remodelada sin producto específico
            raise RuntimeError(f"Error aplicando producto: {response.status_code}")