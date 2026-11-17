"""Arma un borrador de clinicas.json a partir de la web de una clínica.

1. Recorre la web: mapa del sitio + links del inicio, sin blog ni otros idiomas (unas 25 páginas).
   Descarga sin navegador y pasa a Playwright si hay captcha o la página viene vacía.
2. Saca lo seguro sin modelo de lenguaje: links tel:/mailto:/WhatsApp, schema.org (JSON-LD), iframes y PDFs.
3. Claude arma la ficha con el texto de las páginas: no inventa, indica la fuente de cada dato,
   separa sedes y reporta contradicciones.
4. Guarda en borradores/ un JSON con una entrada de clinicas.json por sede, más lo que falta revisar.

El borrador NO se agrega solo a clinicas.json: hay que revisarlo, completar lo marcado y copiarlo.

Uso:
  .venv/bin/python extraer_web.py https://www.clinica.com
  .venv/bin/python extraer_web.py https://www.clinica.com --paginas 15 --solo-recorrer
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from typing import Literal, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import anthropic
import requests
from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RAIZ, "ghl-alta"))
import ghl_oauth  # noqa: E402  (solo para leer el .env)

SALIDA = os.path.join(RAIZ, "borradores")
MODELO = "claude-opus-5-5"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_TEXTO_PAGINA = 15000
MAX_TEXTO_TOTAL = 300000

# ---------- 1. Recorrer la web ----------

SALTEAR = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js|ico|xml|docx?)$|/wp-(admin|login|json|content)|/feed/?$"
    r"|/(cart|carrito|checkout|mi-cuenta|my-account)(/|$)|/(tag|etiqueta|author|autor|category|categoria)/"
    r"|/page/\d+|/\d{4}/\d{2}(/|$)|/comments?/", re.I)
# sitemaps hijos de WordPress/Yoast/RankMath: cuáles sirven y cuáles son ruido
SITEMAP_UTIL = re.compile(r"page|pagina|team|equipo|doctor|profesional|servicio|service|tratamiento|clinica|staff", re.I)
SITEMAP_RUIDO = re.compile(r"post|tag|categor|author|autor|product|attachment|blog|news|noticia|portfolio|"
                           r"elementor|template|wpforms|location", re.I)
PRIORIDADES = [  # (patrón en la URL o el texto del link, puntos)
    (r"contact", 100), (r"aviso-?legal|legal|privacidad|politica", 60), (r"equipo|team|doctor|dra?-|profesional|staff"
     r"|quien|nosotros|about", 70), (r"precio|tarifa|financia|presupuesto", 70), (r"faq|pregunta", 70),
    (r"horario|cita|reserva|pedir", 60), (r"urgencia", 60), (r"clinica|instalacion", 50),
    (r"servicio|tratamiento|especialidad", 50), (r"implant|ortodon|endodon|estetic|blanque|periodon|odontopediat"
     r"|protesis|carilla|invisalign|limpieza", 25), (r"blog|noticia|news|articulo", -80), (r"cookies", -60),
]
IDIOMAS = {"en", "fr", "de", "it", "pt", "ca", "eu", "gl", "nl", "ru", "zh", "ar", "va"}


def dominio(u):
    return urlparse(u).netloc.lower().removeprefix("www.")


def normalizar(u):
    u = urldefrag(u)[0].split("?")[0]
    p = urlparse(u)
    ruta = re.sub(r"/+", "/", p.path).rstrip("/") or "/"
    return f"https://{dominio(u)}{ruta}"


def es_desafio(r_status, html):
    """Respuesta de un anti-bots (captcha de SiteGround, Cloudflare...) o página que arma el contenido con JS."""
    if r_status in (202, 403, 503) or re.search(r"sgcaptcha|cf-chl|challenge-platform|captcha", html[:5000], re.I):
        return True
    texto = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
    return len(re.sub(r"\s+", " ", texto)) < 500


class Descargador:
    """Descarga HTML sin navegador; si el sitio lo bloquea, usa Chromium (Playwright) para todo el recorrido."""

    def __init__(self, pw, inicio):
        self.pw, self.navegador, self.pagina = pw, None, None
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
        try:
            r = self.http.get(inicio, timeout=20)
            self.modo = "navegador" if es_desafio(r.status_code, r.text) else "http"
        except requests.RequestException:
            self.modo = "navegador"
        if self.modo == "navegador":
            self.navegador = pw.chromium.launch()
            ctx = self.navegador.new_context(user_agent=UA, locale="es-ES")
            self.pagina = ctx.new_page()
            self.pagina.set_default_timeout(30000)

    def html(self, url):
        """(url final, html) o None."""
        if self.modo == "http":
            try:
                r = self.http.get(url, timeout=20)
            except requests.RequestException:
                return None
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
                return None
            r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
            return r.url, r.text
        try:
            self.pagina.goto(url, wait_until="domcontentloaded")
        except PWError:
            pass  # redirecciones a mitad de carga: se sigue con lo que haya
        # No se espera "networkidle" (hay sitios que nunca dejan de pedir cosas): se espera a que la página tenga
        # texto, porque los captchas de verificación redirigen solos a la página real después de unos segundos.
        ultimo = None
        for _ in range(6):
            time.sleep(2)
            try:
                ultimo = (self.pagina.url, self.pagina.content())
            except PWError:
                continue
            if not es_desafio(200, ultimo[1]) and not re.search(r"captcha|challenge|/\.well-known/", ultimo[0], re.I):
                return ultimo
        return ultimo

    def texto(self, url):
        try:
            if self.modo == "http":
                r = self.http.get(url, timeout=20)
                return r.text if r.status_code < 400 else ""
            r = self.pagina.context.request.get(url, timeout=20000)
            return r.text() if r.ok else ""
        except Exception:
            return ""

    def cerrar(self):
        if self.navegador:
            self.navegador.close()


def leer_sitemaps(desc, base):
    """{url: tipo} con tipo 'util' (páginas, equipo, servicios) o 'post' (blog: solo si falta material)."""
    candidatos = re.findall(r"(?im)^sitemap:\s*(\S+)", desc.texto(urljoin(base, "/robots.txt")))
    candidatos += [urljoin(base, s) for s in ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml")]
    urls, vistos = {}, set()
    for sm in candidatos:
        if sm in vistos or urls:
            continue
        vistos.add(sm)
        locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", desc.texto(sm))
        hijos = [l for l in locs if l.endswith(".xml") or "sitemap" in l.lower()]
        if not hijos:
            urls.update({l: "util" for l in locs})
            continue
        for h in hijos[:15]:
            nombre = h.rsplit("/", 1)[-1]
            tipo = "util" if SITEMAP_UTIL.search(nombre) or not SITEMAP_RUIDO.search(nombre) else "post"
            if re.search(r"tag|categor|author|autor|attachment|elementor|template", nombre, re.I):
                continue
            for l in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", desc.texto(h)):
                urls.setdefault(l, tipo)
    return urls


def puntaje(url, texto_link="", origen="enlace"):
    ruta = urlparse(url).path.lower()
    if ruta == "/":
        return 1000
    s = {"util": 30, "inicio": 40, "enlace": 10, "post": -100}[origen]
    clave = f"{ruta} {texto_link.lower()}"
    s += max([p for patron, p in PRIORIDADES if re.search(patron, clave) and p > 0], default=0)
    s += sum(p for patron, p in PRIORIDADES if re.search(patron, clave) and p < 0)
    s -= 8 * ruta.strip("/").count("/")  # las páginas importantes suelen estar cerca de la raíz
    return s


EXTRAER_JS = """() => {
  const abs = h => { try { return new URL(h, document.baseURI).href } catch (e) { return '' } };
  const links = [...document.querySelectorAll('a[href]')].map(a => ({href: abs(a.getAttribute('href')),
                 raw: a.getAttribute('href') || '', texto: (a.innerText || a.title || '').trim().slice(0, 80)}));
  const iframes = [...document.querySelectorAll('iframe[src], iframe[data-src]')]
                  .map(f => abs(f.getAttribute('src') || f.getAttribute('data-src')));
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent);
  const clon = document.body ? document.body.cloneNode(true) : document.createElement('body');
  clon.querySelectorAll('script,style,noscript,svg,template,[id*=cookie],[class*=cookie],[id*=cmplz],[class*=cmplz]')
      .forEach(e => e.remove());
  // <br>, celdas y bloques como saltos de línea, para que innerText sin layout no pegue palabras
  clon.querySelectorAll('br').forEach(e => e.replaceWith('\\n'));
  clon.querySelectorAll('p,div,li,h1,h2,h3,h4,h5,h6,tr,td,th,section,article,header,footer,dd,dt,address')
      .forEach(e => e.append('\\n'));
  return {titulo: document.title, links, iframes, ld, texto: clon.textContent};
}"""


def extraer(parser, url, html):
    """Analiza el HTML en una página sin JS ni red (rápido y sin efectos), con <base> para resolver links."""
    html = re.sub(r"(?i)<head([^>]*)>", lambda m: f'<head{m.group(1)}><base href="{url}">', html, count=1)
    parser.set_content(html, wait_until="domcontentloaded")
    d = parser.evaluate(EXTRAER_JS)
    lineas = [re.sub(r"[ \t ]+", " ", l).strip() for l in d["texto"].splitlines()]
    d["lineas"] = [l for l in lineas if l]
    return d


def recorrer(inicio, max_paginas=25):
    inicio = inicio if inicio.startswith("http") else f"https://{inicio}"
    res = {"inicio": inicio, "modo": None, "paginas": [], "tel": set(), "mail": set(), "whatsapp": set(),
           "mapas": set(), "pdfs": set(), "externos": set(), "ld": []}
    with sync_playwright() as pw:
        desc = Descargador(pw, inicio)
        res["modo"] = desc.modo
        sin_js = pw.chromium.launch()
        ctx = sin_js.new_context(java_script_enabled=False)
        ctx.route("**/*", lambda ruta: ruta.abort())  # set_content no debe pedir imágenes ni CSS
        parser = ctx.new_page()
        try:
            primera = desc.html(inicio)
            if not primera:
                sys.exit(f"No pude abrir {inicio}")
            base = primera[0]
            dom = dominio(base)
            prefijo_idioma = (re.match(r"^/([a-z]{2})(/|$)", urlparse(base).path) or [None, None])[1]
            print(f"Sitio: {dom} (modo {desc.modo})")

            candidatos = {}  # url normalizada -> (puntaje, url original)

            def agregar(u, texto="", origen="enlace"):
                if dominio(u) != dom or SALTEAR.search(urlparse(u).path):
                    return
                seg = re.match(r"^/([a-z]{2})(/|$)", urlparse(u).path)
                if seg and seg.group(1) in IDIOMAS and seg.group(1) != prefijo_idioma:
                    return
                n = normalizar(u)
                s = puntaje(u, texto, origen)
                if n not in candidatos or candidatos[n][0] < s:
                    candidatos[n] = (s, u)

            sitemap = leer_sitemaps(desc, base)
            print(f"Mapa del sitio: {len(sitemap)} URLs "
                  f"({sum(t == 'util' for t in sitemap.values())} páginas, {sum(t == 'post' for t in sitemap.values())} de blog)")
            for u, tipo in sitemap.items():
                agregar(u, origen=tipo)

            visitados, leidas, pendiente = set(), set(), (base, primera)
            texto_total = 0
            while len(res["paginas"]) < max_paginas:
                if pendiente:
                    url, descarga = pendiente
                    pendiente = None
                else:
                    restantes = [(s, u) for n, (s, u) in candidatos.items() if n not in visitados and s > -50]
                    if not restantes:
                        break
                    _, url = max(restantes)
                    descarga = desc.html(url)
                visitados.add(normalizar(url))
                if not descarga:
                    continue
                url_final, html = descarga
                if dominio(url_final) != dom or normalizar(url_final) in leidas or "/.well-known/" in url_final:
                    continue
                visitados.add(normalizar(url_final))
                leidas.add(normalizar(url_final))
                d = extraer(parser, url_final, html)
                es_inicio = not res["paginas"]
                for l in d["links"]:
                    h, raw = l["href"], l["raw"].strip()
                    if raw.lower().startswith("tel:"):
                        res["tel"].add(re.sub(r"[^\d+]", "", raw[4:]))
                    elif raw.lower().startswith("mailto:"):
                        res["mail"].add(raw[7:].split("?")[0].strip().lower())
                    elif re.search(r"wa\.me/|api\.whatsapp\.com|web\.whatsapp\.com", h):
                        num = re.search(r"wa\.me/(\+?\d+)|phone=(\+?\d+)", h)
                        res["whatsapp"].add(next(g for g in num.groups() if g) if num else h)
                    elif re.search(r"google\.[a-z.]+/maps|maps\.app\.goo\.gl|goo\.gl/maps", h):
                        res["mapas"].add(h)
                    elif h.lower().split("?")[0].endswith(".pdf"):
                        res["pdfs"].add(h)
                    elif h.startswith("http") and dominio(h) != dom:
                        res["externos"].add(dominio(h))
                    elif h.startswith("http"):
                        agregar(h, l["texto"], "inicio" if es_inicio else "enlace")
                for f in d["iframes"]:
                    if f.startswith("http") and dominio(f) != dom:
                        res["externos"].add("iframe:" + dominio(f))
                        if re.search(r"google\.[a-z.]+/maps", f):
                            res["mapas"].add(f)
                for bloque in d["ld"]:
                    try:
                        res["ld"].append(json.loads(bloque))
                    except json.JSONDecodeError:
                        pass
                res["paginas"].append({"url": url_final, "titulo": d["titulo"], "lineas": d["lineas"]})
                texto_total += sum(len(l) for l in d["lineas"])
                print(f"  {len(res['paginas']):2d}. {url_final}")
                if texto_total > MAX_TEXTO_TOTAL:
                    print("  (tope de texto alcanzado)")
                    break
        finally:
            sin_js.close()
            desc.cerrar()
    for k in ("tel", "mail", "whatsapp", "mapas", "pdfs", "externos"):
        res[k] = sorted(x for x in res[k] if x)
    return res


# ---------- 2. Datos seguros (sin modelo de lenguaje) ----------

TIPOS_NEGOCIO = re.compile(r"Dentist|LocalBusiness|MedicalBusiness|MedicalClinic|MedicalOrganization|Organization|"
                           r"HealthAndBeautyBusiness|Physician", re.I)
SOCIALES = re.compile(r"facebook|instagram|twitter|x\.com|linkedin|youtube|tiktok|pinterest|fonts\.|gstatic|"
                      r"googleapis|wordpress|w\.org|gravatar|elementor|wp\.com|schema\.org|jquery|cloudflare", re.I)


def nodos_ld(ld):
    pila, salida = list(ld), []
    while pila:
        n = pila.pop()
        if isinstance(n, list):
            pila.extend(n)
        elif isinstance(n, dict):
            if "@graph" in n:
                pila.extend(n["@graph"] if isinstance(n["@graph"], list) else [n["@graph"]])
            tipo = n.get("@type")
            tipo = " ".join(tipo) if isinstance(tipo, list) else str(tipo or "")
            if TIPOS_NEGOCIO.search(tipo) or tipo == "FAQPage":
                salida.append(n)
    return salida


def datos_seguros(rec):
    vistos, ld = set(), []
    for n in nodos_ld(rec["ld"]):
        clave = json.dumps(n, sort_keys=True, ensure_ascii=False)
        if clave not in vistos:
            vistos.add(clave)
            ld.append(n)
    tld = dominio(rec["inicio"]).rsplit(".", 1)[-1]
    return {
        "telefonos_en_links": rec["tel"], "emails_en_links": rec["mail"], "whatsapp": rec["whatsapp"],
        "mapas": rec["mapas"][:5], "pdfs": rec["pdfs"][:20],
        "servicios_externos": [e for e in rec["externos"] if not SOCIALES.search(e)][:30],
        "schema_org": ld[:10],
        "pais_por_dominio": {"es": "ES", "ar": "AR", "mx": "MX", "cl": "CL", "uy": "UY", "co": "CO", "pe": "PE"}.get(tld),
    }


def texto_para_modelo(rec):
    """Texto por página. Las líneas que se repiten en casi todas (menú, pie de página) van una sola vez arriba:
    ahí suelen estar dirección, teléfono y horario, así que no se descartan."""
    n = len(rec["paginas"])
    cuenta = {}
    for p in rec["paginas"]:
        for l in set(p["lineas"]):
            cuenta[l] = cuenta.get(l, 0) + 1
    comun = {l for l, c in cuenta.items() if n >= 3 and c >= max(3, 0.6 * n)}
    orden_comun = []
    for p in rec["paginas"]:
        for l in p["lineas"]:
            if l in comun and l not in orden_comun:
                orden_comun.append(l)
    partes = ["### Texto que se repite en todas las páginas (cabecera, menú y pie de página)\n" + "\n".join(orden_comun)]
    for p in rec["paginas"]:
        cuerpo, previa = [], None
        for l in p["lineas"]:
            if l not in comun and l != previa:
                cuerpo.append(l)
            previa = l
        partes.append(f"### Página: {p['url']}\nTítulo: {p['titulo']}\n" + "\n".join(cuerpo)[:MAX_TEXTO_PAGINA])
    return "\n\n".join(partes)


# ---------- 3. Claude arma la ficha ----------

# Esquema chico a propósito: la salida estructurada compila el esquema a una gramática y rechaza las muy grandes.
# Por eso los datos faltantes van como "" (no null) y el horario es una lista plana de tramos.

class Tramo(BaseModel):
    dia: str = Field(description="lunes, martes, miercoles, jueves, viernes, sabado o domingo")
    desde: str = Field(description="HH:MM 24 h")
    hasta: str = Field(description="HH:MM 24 h")


class Sede(BaseModel):
    nombre: str
    direccion: str = Field(description="Calle y número, sin ciudad")
    ciudad: str
    provincia: str
    cp: str
    pais: str = Field(description="ISO 2 letras")
    telefono: str = Field(description="Formato internacional")
    whatsapp: str
    email: str
    horario: list[Tramo] = Field(description="Un tramo por día y turno. Vacío si la web no publica horario")
    horario_texto: str = Field(description="El horario como lo escribe la web")
    timezone: str = Field(description="IANA, deducida de ciudad y país")


class Profesional(BaseModel):
    nombre: str
    apellido: str
    titulo: str = Field(description="Dr., Dra., etc.")
    especialidad: str
    email: str
    telefono: str
    es_director: bool
    sede: str = Field(description="Nombre de la sede donde atiende, vacío si atiende en todas o no se sabe")


class Servicio(BaseModel):
    nombre: str
    precio: str = Field(description="Con moneda, tal como figura. Vacío si no figura")


class Pregunta(BaseModel):
    pregunta: str
    respuesta: str = Field(description="1 o 2 frases")


class Fuente(BaseModel):
    campo: str = Field(description="Ej. sedes[0].telefono")
    url: str


class Ficha(BaseModel):
    clinica: str = Field(description="Nombre comercial")
    razon_social: str
    sedes: list[Sede]
    profesionales: list[Profesional]
    servicios: list[Servicio]
    faq: list[Pregunta]
    urgencias_telefono: str = Field(description="Teléfono específico de urgencias, si la web lo da")
    urgencias_texto: str = Field(description="Lo que dice la web sobre urgencias")
    asistente_nombre: str = Field(description="Nombre femenino corto y común en el país")
    asistente_idioma: str = Field(description="es-ES para España, es-419 para Latinoamérica")
    asistente_saludo: str = Field(description="Saludo telefónico breve con el nombre de la clínica y del asistente, "
                                              "sin 'buenos días' ni 'buenas tardes' (se usa a cualquier hora)")
    sistemas_de_turnos: list[str] = Field(description="Plataformas externas de turnos detectadas")
    faltantes: list[str] = Field(description="Datos que el alta necesita y la web no publica")
    contradicciones: list[str] = Field(description="Campo, valores con su URL y cuál se eligió")
    fuentes: list[Fuente]
    observaciones: list[str]


INSTRUCCIONES = """Sos un asistente que arma la ficha de una clínica dental a partir de su sitio web, para configurar \
su recepcionista telefónica virtual. Recibís datos extraídos de forma automática (links de teléfono y email, \
schema.org) y el texto de las páginas, cada una con su URL.

Reglas:
- No inventes nada. Si un dato no figura, va "" (o lista vacía) y se anota en "faltantes".
- Cada dato que cargues tiene que tener su URL en "fuentes". El texto "que se repite en todas las páginas" es la \
cabecera y el pie de página: como fuente usá la URL de la primera página.
- Si la clínica tiene varias sedes, cada una va por separado en "sedes" con su dirección, teléfono, email y horario.
- Si un dato aparece con valores distintos en distintas páginas, preferí en este orden: schema.org, página de \
contacto, pie de página, inicio, y el resto. Reportá la diferencia en "contradicciones".
- Horario: un tramo por día y turno; los días cerrados no llevan tramos. "Lunes a viernes de 9 a 14 y de 16 a 20" \
son dos tramos para cada día de lunes a viernes. Si la web no publica horario, la lista va vacía.
- Teléfonos en formato internacional (+34..., +54...). El país sale de la dirección, el prefijo o el dominio.
- Servicios: los tratamientos que ofrece, con el precio solo si la web lo publica.
- Profesionales: solo odontólogos y personal clínico con nombre. Marcá quién es el director o titular si se dice.
- "faltantes" debe incluir siempre los datos que el alta necesita y no están: email de la clínica, horario, \
profesional, precios, etc.
- El agente (nombre, idioma y saludo) es una sugerencia tuya: idioma es-ES para España y es-419 para Latinoamérica.
- Escribí en español."""


def armar_ficha(rec, seguros):
    client = anthropic.Anthropic()
    contenido = (f"Web: {rec['inicio']}\n\n## Datos extraídos automáticamente\n"
                 f"{json.dumps(seguros, ensure_ascii=False, indent=1)}\n\n## Texto de las páginas\n\n"
                 f"{texto_para_modelo(rec)}")
    print(f"Consultando a Claude ({MODELO}, ~{len(contenido) // 4000}k tokens de entrada)...", flush=True)
    # con streaming: el SDK no deja hacer pedidos largos sin stream (riesgo de timeout HTTP)
    with client.beta.messages.stream(
        model=MODELO,
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # si el modelo declina, el servidor reintenta con otro
        output_config={"effort": "medium"},
        system=INSTRUCCIONES,
        messages=[{"role": "user", "content": contenido}],
        output_format=Ficha,
    ) as stream:
        r = stream.get_final_message()
    if r.stop_reason == "refusal":
        sys.exit("Claude no respondió (refusal).")
    ficha = next((b.parsed_output for b in r.content if b.type == "text"), None)
    if r.stop_reason == "max_tokens" or ficha is None:
        sys.exit(f"Respuesta incompleta de Claude (stop_reason={r.stop_reason}).")
    u = r.usage
    print(f"  tokens: entrada {u.input_tokens}, salida {u.output_tokens}")
    return ficha


# ---------- 4. Borrador de clinicas.json ----------

VOZ = {"es-ES": "cartesia-Isabel", "es-419": "cartesia-Hailey-Spanish-latin-america"}
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
REVISAR = "COMPLETAR"


def slug(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40].strip("-")


def horario_dict(tramos):
    h = {}
    for t in tramos:
        dia = unicodedata.normalize("NFKD", t.dia).encode("ascii", "ignore").decode().lower().strip()
        if dia in DIAS and re.fullmatch(r"\d{1,2}:\d{2}", t.desde) and re.fullmatch(r"\d{1,2}:\d{2}", t.hasta):
            h.setdefault(dia, []).append([t.desde.zfill(5), t.hasta.zfill(5)])
    return {d: sorted(h[d]) for d in DIAS if d in h}


def texto_horario(horario):
    def hora(t):
        hh, mm = t.split(":")
        return str(int(hh)) if mm == "00" else f"{int(hh)}:{mm}"
    grupos = []
    for d in DIAS:
        tramos = horario.get(d)
        if tramos and grupos and grupos[-1][1] == tramos and DIAS.index(grupos[-1][0][-1]) == DIAS.index(d) - 1:
            grupos[-1][0].append(d)
        elif tramos:
            grupos.append([[d], tramos])
    sep = {1: "", 2: " y "}
    return "; ".join((g[0][0] if len(g[0]) == 1 else f"{g[0][0]}{sep.get(len(g[0]), ' a ')}{g[0][-1]}") + " de " +
                     " y de ".join(f"{hora(a)} a {hora(b)}" for a, b in g[1]) + " h" for g in grupos).capitalize()


def tel(t):
    return re.sub(r"[^\d+]", "", t or "")


def a_clinicas(f, web):
    """Una entrada de clinicas.json por sede. Lo que falta queda como "COMPLETAR"."""
    salida, slugs = [], set()
    for s in f.sedes:
        if len(f.sedes) == 1:
            nombre = f.clinica
        else:
            nombre = s.nombre if f.clinica.lower() in s.nombre.lower() else f"{f.clinica} - {s.nombre}"
        if len(f.sedes) == 1:
            base = slug(nombre)
        else:  # ej. vilches-y-ahumada-dentistas-mairena
            lugar = re.sub(r"\b(clinica|sede|centro|dental|de|del|la|el)\b", " ", slug(s.nombre).replace("-", " "))
            base = f"{slug(f.clinica)[:28].strip('-')}-{slug(lugar)[:12]}".strip("-")
        sl, i = base, 2
        while sl in slugs:
            sl, i = f"{base[:36]}-{i}", i + 1
        slugs.add(sl)
        # profesional del alta: el titular de esta sede, o el de la clínica, o el primero
        de_sede = [p for p in f.profesionales if not p.sede or p.sede.lower() in s.nombre.lower()
                   or s.nombre.lower() in p.sede.lower()] or f.profesionales
        director = next((p for p in de_sede if p.es_director and p.sede), None) or \
            next((p for p in de_sede if p.es_director), de_sede[0] if de_sede else None)
        horario = horario_dict(s.horario)
        prof = {"nombre": director.nombre, "apellido": director.apellido or REVISAR,
                "email": director.email or REVISAR, "telefono": tel(director.telefono or s.telefono) or REVISAR} \
            if director else {"nombre": REVISAR, "apellido": REVISAR, "email": REVISAR, "telefono": REVISAR}
        nombre_prof = f"{director.titulo or ''} {director.nombre} {director.apellido or ''}".strip() \
            if director else REVISAR
        urgencias = ({"accion": "derivar", "telefono": tel(f.urgencias_telefono),
                      "texto": f"Si es una urgencia, llame al {f.urgencias_telefono}."} if f.urgencias_telefono else
                     {"accion": "tomar_datos",
                      "texto": "Si es una urgencia, tomamos sus datos y la clínica lo llama a la brevedad."})
        idioma = f.asistente_idioma if f.asistente_idioma in VOZ else ("es-ES" if s.pais == "ES" else "es-419")
        salida.append({
            "slug": sl,
            "clinica": nombre,
            "perfil": {
                "telefono": tel(s.telefono) or REVISAR, "email": s.email or REVISAR,
                "direccion": s.direccion or REVISAR, "ciudad": s.ciudad or REVISAR,
                "provincia": s.provincia or s.ciudad or REVISAR, "pais": s.pais or REVISAR, "cp": s.cp or REVISAR,
                "web": web, "timezone": s.timezone,
                "contacto": {"nombre": REVISAR, "apellido": REVISAR, "email": REVISAR},
            },
            "profesional": prof,
            "calendario": {"nombre": "Primera visita" if idioma == "es-ES" else "Primera consulta",
                           "duracion_min": 30, "horario": horario or REVISAR},
            "custom_values": {
                "Direccion": ", ".join(x for x in (s.direccion, s.ciudad) if x) or REVISAR,
                "Telefono": tel(s.telefono) or REVISAR,
                "Horario": texto_horario(horario) if horario else REVISAR,
                "Profesional": nombre_prof,
            },
            "pipeline": {"nombre": "Pacientes", "etapas": ["Nuevo contacto", "Turno agendado", "Asistio", "No asistio"]},
            "servicios": [{"nombre": x.nombre, "precio": x.precio or "Consultar en la clinica"} for x in f.servicios],
            "faq": [{"pregunta": q.pregunta, "respuesta": q.respuesta} for q in f.faq],
            "urgencias": urgencias,
            "agente": {"nombre_asistente": f.asistente_nombre, "idioma": idioma,
                       "voz": VOZ[idioma], "saludo": f.asistente_saludo},
        })
    return salida


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="web de la clínica")
    ap.add_argument("--paginas", type=int, default=25, help="máximo de páginas a leer (por defecto 25)")
    ap.add_argument("--solo-recorrer", action="store_true", help="recorre y guarda el texto, sin consultar a Claude")
    args = ap.parse_args()

    ghl_oauth.cargar_env(() if args.solo_recorrer else ("ANTHROPIC_API_KEY",))
    os.makedirs(SALIDA, exist_ok=True)

    rec = recorrer(args.url, args.paginas)
    seguros = datos_seguros(rec)
    base = os.path.join(SALIDA, dominio(rec["inicio"]))
    with open(base + ".recorrido.json", "w") as fh:
        json.dump({**rec, "datos_seguros": seguros}, fh, ensure_ascii=False, indent=1)
    print(f"Recorrido guardado: {base}.recorrido.json")
    if args.solo_recorrer:
        return

    ficha = armar_ficha(rec, seguros)
    clinicas = a_clinicas(ficha, rec["inicio"])
    defecto = ["calendario.duracion_min = 30 (la web no lo dice)", "agente (nombre, saludo y voz) sugerido",
               "perfil.timezone deducida de la ciudad", "perfil.contacto: dueño de la subcuenta de GHL",
               "urgencias.accion según lo que publica la web"]
    borrador = {
        "web": rec["inicio"],
        "fecha": time.strftime("%Y-%m-%d"),
        "clinicas": clinicas,
        "revisar": {
            "completar": sorted({f"{c['slug']}: {ruta}" for c in clinicas for ruta in _completar(c)}),
            "valores_por_defecto": defecto,
            "faltantes": ficha.faltantes,
            "contradicciones": ficha.contradicciones,
            "observaciones": ficha.observaciones,
        },
        "extra": {
            "razon_social": ficha.razon_social,
            "profesionales": [p.model_dump() for p in ficha.profesionales],
            "sedes": [s.model_dump() for s in ficha.sedes],
            "urgencias_web": {"telefono": ficha.urgencias_telefono, "texto": ficha.urgencias_texto},
            "sistemas_de_turnos": ficha.sistemas_de_turnos,
            "pdfs": seguros["pdfs"],
        },
        "fuentes": [x.model_dump() for x in ficha.fuentes],
    }
    with open(base + ".json", "w") as fh:
        json.dump(borrador, fh, ensure_ascii=False, indent=2)

    print(f"\n=== {ficha.clinica}: {len(clinicas)} sede(s), {len(ficha.profesionales)} profesionales, "
          f"{len(ficha.servicios)} servicios, {len(ficha.faq)} preguntas frecuentes")
    r = borrador["revisar"]
    for titulo, items in (("Completar", r["completar"]), ("Faltan en la web", r["faltantes"]),
                          ("Contradicciones", r["contradicciones"]),
                          ("Observaciones", r["observaciones"])):
        if items:
            print(f"{titulo}:")
            for i in items:
                print(f"  - {i}")
    print(f"\nBorrador: {base}.json")
    print("Revisalo, completá lo marcado como COMPLETAR y copiá las entradas de \"clinicas\" a clinicas.json.")


def _completar(obj, ruta=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _completar(v, f"{ruta}.{k}" if ruta else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _completar(v, f"{ruta}[{i}]")
    elif obj == REVISAR:
        yield ruta


if __name__ == "__main__":
    main()
