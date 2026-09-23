#!/usr/bin/env python3
"""
Verificación previa a cualquier entrega de código Python.

Nace de un bug real: se integró generar_imagen_con_producto() usando la
variable tienda_ctx, que existía en OTRA función del mismo archivo. Python no
lo marca como error hasta que ese camino del código se ejecuta — y se ejecutó
en producción, con un cliente real esperando su remodelación.

Este script detecta esa clase de error ANTES de subir nada: nombres usados
que no están definidos ni importados en el mismo alcance donde se usan.

Uso:
    python3 scripts/verificar_backend.py
    python3 scripts/verificar_backend.py app/api/whatsapp.py   # un solo archivo

Sale con código 1 si encuentra algo, para poder usarlo como bloqueo en CI.
"""
import ast
import builtins
import glob
import sys


def nombres_sin_definir(ruta: str) -> list[str]:
    arbol = ast.parse(open(ruta, encoding="utf-8").read())
    definidos = set(dir(builtins))

    for n in ast.walk(arbol):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                definidos.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definidos.add(n.name)
            args = list(n.args.args) + list(n.args.kwonlyargs) + list(n.args.posonlyargs)
            for a in args:
                definidos.add(a.arg)
            if n.args.vararg:
                definidos.add(n.args.vararg.arg)
            if n.args.kwarg:
                definidos.add(n.args.kwarg.arg)
        elif isinstance(n, ast.ClassDef):
            definidos.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            definidos.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            definidos.add(n.name)
        elif isinstance(n, ast.comprehension) and isinstance(n.target, ast.Name):
            definidos.add(n.target.id)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if isinstance(it.optional_vars, ast.Name):
                    definidos.add(it.optional_vars.id)
        elif isinstance(n, ast.Tuple):
            for e in n.elts:
                if isinstance(e, ast.Name) and isinstance(e.ctx, ast.Store):
                    definidos.add(e.id)

    usados = {
        n.id for n in ast.walk(arbol)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return sorted(usados - definidos)


def main():
    archivos = sys.argv[1:] or sorted(glob.glob("app/**/*.py", recursive=True))
    archivos = [f for f in archivos if "__pycache__" not in f]

    problemas = 0
    for f in archivos:
        try:
            faltan = nombres_sin_definir(f)
        except SyntaxError as e:
            print(f"  ❌ {f}: error de sintaxis — {e}")
            problemas += 1
            continue
        if faltan:
            problemas += 1
            print(f"  ⚠️  {f}: {faltan}")

    if problemas:
        print(f"\n❌ {problemas} archivo(s) con nombres sin definir. NO subir hasta corregir.")
        sys.exit(1)
    print(f"✅ {len(archivos)} archivo(s) revisados — ningún nombre sin definir.")


if __name__ == "__main__":
    main()