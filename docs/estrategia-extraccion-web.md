# Estrategia para extraer la información de una clínica desde su web

Basada en el análisis de 5 webs reales de clínicas dentales de España (17/11/2026): miboalmeria.es, clinicadentaldavident.es, pullasdental.com, vadentistas.com y olgalanza.es.

## Qué encontramos

| | Mibo (Almería) | Davident (Córdoba) | Pullas (Granada) | Vadentistas (Sevilla) | Olga Lanza (Teruel) |
|---|---|---|---|---|---|
| Tecnología | WordPress, captcha de SiteGround | WordPress (Divi) | WordPress | WordPress | WordPress (Elementor) |
| Se lee sin navegador | **No**: el captcha devuelve 168 bytes | Sí | Sí | Sí | Sí |
| Mapa del sitio | 201 URLs (casi todo blog) | páginas y blog separados | páginas y blog separados | páginas, blog y **equipo** separados | páginas y blog separados |
| Datos para Google (schema.org) | No | Solo el nombre | **Sí**: `Dentist` con dirección, teléfono y horarios; también `FAQPage` | No | No |
| Teléfono | Sí | 2 (fijo y móvil) | Sí + WhatsApp | Uno por sede, como texto y no como link | Fijo + WhatsApp |
| Email | **No figura** | Sí | Solo en "Aviso legal" | Uno por sede | Sí (gmail) |
| Dirección | Página de contacto | Pie de página y contacto | schema.org y contacto | **2 sedes** | Pie de página, contacto y aviso legal |
| Horario | Distinto cada día, con turno partido | Distinto cada día, con turno partido | Distinto según el día | Turno partido | **Se contradice** entre páginas |
| Profesionales | Página por profesional | Solo mencionados en el texto | En "Quiénes somos" | Página por profesional (6) | Página "Equipo" |
| Precios | No | En las preguntas frecuentes (ortodoncia 2060–3700 €, implante ~600 €) | "Implantes desde 750 €" | No | No |
| Preguntas frecuentes | No | Página propia | En schema.org | Página propia | No |
| Urgencias | Solo en reseñas | "Servicio de urgencias" | No | No | No |

## Lecciones

1. **No hay dos webs iguales.** Ni los nombres de las páginas, ni dónde están los datos, ni qué datos publican. No conviene escribir reglas por sitio.
2. **Lo más valioso está en el marco de la página**: pie de página, cabecera, barra lateral y páginas legales. Ahí aparecen dirección, teléfonos, email, horario y razón social. Al quitar el menú y el pie de página para quedarse con el "contenido", esos datos se pierden.
3. **El blog es ruido.** En Mibo, 200 de 201 URLs son artículos. Hay que excluirlo o el límite de páginas se gasta en notas de blog.
4. **El mapa del sitio de WordPress ya clasifica las URLs.** Separa páginas, artículos, profesionales, etiquetas y autores, y eso permite elegir qué recorrer sin adivinar por la URL.
5. **Los datos para Google (schema.org) son la mejor fuente cuando existen**, pero solo 1 de 5 sitios los tiene completos.
6. **Hay datos viejos o contradictorios.** En Olga Lanza el horario cambia según la página, y la web tiene noticias de 2020. Hay que priorizar contacto, pie de página e inicio por sobre páginas sueltas, y avisar si hay contradicciones.
7. **Una web puede ser de varias sedes**, como Vadentistas con Sevilla y Mairena. Cada sede tiene su teléfono, email y dirección.
8. **El formato de `clinicas.json` se queda corto para el horario.** Las clínicas reales tienen horarios distintos según el día y turno partido (9:30–13:30 / 16:30–20:30), y hoy solo soportamos un único "abre" y "cierra" para todos los días.
9. **Algunos sitios necesitan un navegador real** por un captcha o por contenido cargado con JavaScript. Además, hay que esperar a que el DOM esté cargado, no a que la red quede quieta: Davident nunca deja de pedir cosas.

## Estrategia propuesta

### 1. Descargar
- Probar primero sin navegador, que es rápido.
- Pasar a Playwright si la respuesta parece una verificación anti-bots o casi no tiene texto: HTTP 202, `sgcaptcha`, Cloudflare o menos de 500 caracteres.
- Esperar a que cargue el DOM y unos segundos más, sin esperar a que la red quede quieta.

### 2. Elegir qué páginas leer (unas 25)
1. **Mapa del sitio**: `robots.txt` → `sitemap_index.xml` / `wp-sitemap.xml`. Usar las páginas y los profesionales; descartar artículos, etiquetas, categorías y autores.
2. **Links del menú y del pie de página** de la página principal, porque las páginas importantes siempre están enlazadas ahí.
3. **Normalizar las URLs**: http/https, con y sin `www`, `/` final, `#ancla`. Descartar las versiones en otros idiomas (`/en/`) y los archivos por fecha (`/2020/10`).
4. **Ordenar por utilidad**:
   - contacto, aviso legal, inicio, equipo y profesionales, servicios y tratamientos, precios, preguntas frecuentes, la clínica / nosotros, cita
   - el resto después
   - si no queda otra opción, artículos del blog

### 3. Extraer de cada página
- **Todo el texto, incluidos el pie de página y la cabecera.** Los bloques que se repiten en todas las páginas se guardan una sola vez.
- **Links `tel:`, `mailto:`, `wa.me`** y de Google Maps.
- **Datos para Google (schema.org, JSON-LD)**.
- **iframes de otras empresas**, para detectar sistemas de turnos como Doctoralia.
- **PDFs** encontrados. Se anotan; podrían leerse con Docling si hiciera falta.

### 4. Datos que se leen sin modelo de lenguaje (seguros)
- Lo que traiga schema.org (`Dentist` / `LocalBusiness`).
- Teléfonos y emails de los links.
- Formato de teléfono y país a partir del dominio y del prefijo.

### 5. El modelo de lenguaje arma la ficha
Recibe:
- el texto agrupado por página, con su URL
- los datos seguros del paso 4
- el esquema de `clinicas.json`

Reglas que se le dan:
- No inventar: si un dato no figura, va `null`.
- Para cada dato, indicar **de qué URL lo sacó**.
- Si hay **varias sedes**, devolverlas por separado.
- Si un dato se contradice entre páginas, preferir contacto, pie de página e inicio, y **reportar la contradicción**.
- Proponer lo que no está en la web pero el agente necesita: nombre del asistente, saludo e idioma. Esos datos se marcan como "sugerido".

### 6. Resultado: un borrador para revisar
Un JSON con el formato de `clinicas.json`, más tres listas:
- **`faltantes`**: por ejemplo email, precios o datos del profesional.
- **`contradicciones`**: por ejemplo el horario de Olga Lanza.
- **`fuentes`**: la URL de donde salió cada dato.

Los datos que ninguna web publica llevan un valor por defecto marcado para revisar:
- **Duración del turno:** 30 minutos.
- **Voz del agente:** según el país.
- **Zona horaria:** según la ciudad y el país.
- **Email del profesional:** si no figura, lo tiene que completar la agencia.

Nadie da de alta una clínica con datos que no revisó.

## Decisiones tomadas
- **Modelo de lenguaje:** Claude (`claude-opus-5-5`) por API, con salida estructurada (esquema Pydantic).
- **Horario por día:** `calendario.horario` en `clinicas.json` tiene uno o más tramos por día. Se aplica al calendario de GHL, al horario del profesional y al prompt.
- **Varias sedes:** el extractor devuelve una entrada de `clinicas.json` por sede, y cada sede se da de alta como una subcuenta propia. Se puede cambiar más adelante.

Implementación: [`extraer_web.py`](../extraer_web.py).
