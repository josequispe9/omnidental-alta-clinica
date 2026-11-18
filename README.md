# Alta de clínicas dentales: GHL + Retell (demo)

Da de alta una clínica dental con un solo comando:

- **GHL (GoHighLevel):** configura la subcuenta de la clínica (perfil, profesional, calendario, notificaciones por email, embudo de pacientes y un menú para ver las llamadas).
- **Retell:** crea el agente de voz de la clínica, con su prompt, su voz y sus herramientas.
- **Middleware (Python, FastAPI):** conecta los dos. El agente lo usa para consultar disponibilidad, registrar pacientes, agendar turnos y enviar correos. Al terminar cada llamada guarda el resumen en GHL.

```
[Retell] <-> [Middleware local + ngrok] <-> [GHL]
```

El ID de cada clínica es el `locationId` de su subcuenta de GHL. Un solo middleware atiende a todas las clínicas.

Los documentos de diseño están en [`docs/`](docs/): las instrucciones originales y el plan por etapas.

## Estructura

```
datos.py                 # base de datos (Postgres en Supabase): única puerta a los datos; listar/exportar/importar
admin/                   # panel de administración: colas de extracción y alta, revisión y avance
progreso.py              # eventos de avance de los scripts (texto en terminal, JSON para el panel)
extraer_web.py           # arma el borrador de una clínica leyendo su web (Playwright + Claude)
crear_subcuenta.py       # paso 0: crea la subcuenta de GHL en el navegador (Playwright + Brave)
alta.py                  # alta completa de una clínica (GHL + Retell + registro)
clinicas.example.json    # ejemplo del formato de una clínica (para datos.py importar)
ghl-alta/
  alta_ghl.py            # pasos del alta en GHL
  ghl_oauth.py           # app de Marketplace: token de cualquier subcuenta
middleware/
  app.py                 # servidor: herramientas del agente y webhook de fin de llamada
  ghl.py                 # funciones de GHL (contacto, disponibilidad, cita, correo, nota)
  llamadas.py            # páginas de llamadas (transcripción y grabación leídas de Retell)
  prompt.py              # prompt del agente a partir de los datos de la clínica
  retell_alta.py         # crea o actualiza el agente de Retell
  probar_ghl.py          # prueba las funciones de GHL y borra lo que crea
```

## Datos

Todo vive en Postgres, en el proyecto de Supabase `omnidental-demo` (us-east-1), schema `omnidental`. Se accede solo por `datos.py`, con la conexión `SUPABASE_DB_URL` del `.env`:

| Tabla | Qué guarda |
|---|---|
| `clinicas` | Una fila por clínica o sede, de punta a punta: <ul><li>los datos de la clínica, como JSON en el formato de `clinicas.example.json`</li><li>el estado en el panel</li><li>los IDs de GHL y de Retell</li><li>el secreto del agente y el de la página de llamadas</li></ul> |
| `eventos` | El registro de actividad de cada clínica |
| `llamadas` | El contacto de GHL de cada llamada de Retell, para la nota de fin de llamada |
| `tokens` | Los tokens OAuth de la app de Marketplace de GHL |

```bash
.venv/bin/python datos.py listar                          # clínicas, estado y subcuenta
.venv/bin/python datos.py exportar <slug> clinica.json    # para editar una clínica a mano...
.venv/bin/python datos.py importar clinica.json           # ...y volver a cargarla (o una lista de clínicas)
```

**Conexión:** `SUPABASE_DB_URL` es la cadena del **Session pooler**, que está en Supabase → Connect. La conexión directa de Supabase solo funciona por IPv6.

**Seguridad:** la base guarda secretos. Por eso el schema `omnidental` no está expuesto en la API pública de Supabase y tiene RLS sin políticas: los roles `anon` y `authenticated` no tienen acceso, y solo se lee con la conexión directa. La estructura está en la migración `esquema_omnidental`, aplicada en Supabase.

**Rendimiento:**
- Cada proceso reutiliza sus conexiones.
- La cola toma la siguiente clínica en una sola operación atómica (`FOR UPDATE SKIP LOCKED`), así dos procesos no agarran la misma.
- El middleware guarda en memoria por 30 s la relación agente → subcuenta y los datos de cada subcuenta, que solo cambian con un alta.
- Los access token de cada subcuenta se guardan en memoria hasta que vencen. Los refresh tokens siempre se leen de la base, porque GHL los rota.

Mientras el middleware corra en tu máquina, cada consulta a la base tarda unos 0,4 s. Una herramienta del agente tarda unos 4 s la primera vez y alrededor de 1 s las siguientes. En la nube, en la misma región que la base, la latencia de la base baja a milisegundos.

Archivos locales que **no** se suben: `.env`, `borradores/` y los logs. La base SQLite anterior se copia a Supabase con `datos.py migrar-sqlite <archivo>`.

## Configuración (una sola vez)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                       # completar los valores
cd ghl-alta && ../.venv/bin/python ghl_oauth.py instalar   # instala la app de Marketplace en la agencia
```

Al instalar la app, elegí la agencia y marcá que se instale en todas las subcuentas, también las futuras.

## Panel de administración (recomendado)

Hace todo el circuito desde el navegador:
1. Pegás varias webs.
2. Se extraen sus datos.
3. Revisás y completás cada clínica.
4. Se da de alta: subcuenta + GHL + agente de Retell.

```bash
brave-browser --remote-debugging-port=9222          # con la sesión de GHL iniciada (para crear subcuentas)
.venv/bin/uvicorn admin.app:app --host 127.0.0.1 --port 8001
# abrir http://127.0.0.1:8001
```

**Colas:**
- Hay dos colas y cada una procesa de a una clínica: **extracción** y **alta**.
- Mientras revisás una clínica, las extracciones siguientes avanzan solas.
- Las altas corren estrictamente de a una, porque hay un solo Brave.

**Revisión:**
- Formulario por secciones, con la grilla del horario por día y la vista previa del prompt.
- Los campos `COMPLETAR` se marcan en rojo. Al lado se ven lo que falta, las contradicciones, las observaciones y la página de donde salió cada dato.
- El servidor no deja validar si falta algo:
  - emails y teléfonos mal escritos;
  - zona horaria inválida;
  - tramos de horario mal cargados;
  - un slug repetido.

**Validar y dar de alta:**
- Se guardan los datos revisados y se encola el alta.
- Corre `crear_subcuenta.py <slug> --alta`, el mismo comando de la terminal.

**Avance:**
- Cada clínica muestra la etapa, el paso y un registro de actividad: extracción → subcuenta → GHL → Retell.
- Si falla, se ve el error y la captura del navegador, y se puede reintentar sin duplicar nada.
- Al terminar aparecen los links a la subcuenta, al agente y a la página de llamadas.

**Estado del sistema:** arriba se muestra si responden Brave, el middleware y ngrok, si faltan claves en `.env` y cuántas clínicas hay en cada cola.

**Seguridad:** el panel escucha solo en `127.0.0.1`. **No lo publiques con ngrok**: crea subcuentas y gasta créditos.

El estado se guarda en la misma base de Supabase. Las clínicas cargadas desde la terminal también aparecen en el panel. Si el panel se reinicia mientras algo corre, esa clínica queda con error ("interrumpida") y se reintenta desde la lista.

Los scripts emiten eventos con `progreso.py`. En la terminal muestran el mismo texto de siempre; con `ALTA_EVENTOS=json`, que es como los corre el panel, emiten líneas JSON.

## Cargar los datos de la clínica desde su web (terminal)

```bash
.venv/bin/python extraer_web.py https://www.clinica.com
```

- Recorre la web: unas 25 páginas útiles (contacto, aviso legal, equipo, servicios, precios, preguntas frecuentes), sin blog ni otros idiomas. Si la web tiene captcha, usa un navegador.
- Toma los teléfonos, emails y WhatsApp de los links y los datos de schema.org. Con eso y el texto de las páginas, Claude arma la ficha sin inventar datos.
- Guarda `borradores/<dominio>.json` con una clínica por sede. Además incluye:
  - lo que hay que completar (marcado como `COMPLETAR`)
  - los datos que la web no publica
  - las contradicciones entre páginas
  - la URL de donde salió cada dato
- Con `--importar`, carga cada sede en la base como "por revisar", y se revisa y se da de alta desde el panel. Sin esa opción, solo deja el archivo.

Detalles de la estrategia en [`docs/estrategia-extraccion-web.md`](docs/estrategia-extraccion-web.md).

### Horario

`calendario.horario` tiene un horario por día, con uno o más tramos, para cubrir el turno partido. Los días que no figuran son días cerrados:

```json
"horario": {"lunes": [["09:30", "13:30"], ["16:30", "20:30"]], "viernes": [["09:30", "14:00"]]}
```

El alta lo carga en el calendario de GHL y en el horario de trabajo del profesional, y el agente lo recibe en su prompt.

### Campos opcionales (los completa `extraer_web.py`)

| Campo | Para qué |
|---|---|
| `equipo` | Profesionales con título y especialidad. El agente responde quién atiende cada especialidad. Los turnos se siguen agendando en el calendario único. |
| `informacion_util` | Estacionamiento, financiación, seguros, idiomas, primera visita gratis... El agente lo usa para responder. |
| `contacto_extra` | WhatsApp y otros teléfonos. Van al prompt. |
| `custom_values.Email` / `.WhatsApp` | Se crean como custom values en GHL. |

Además, el agente de Retell recibe como `boosted_keywords` los nombres de la clínica, los profesionales, la calle y los tratamientos, para que el reconocimiento de voz los transcriba bien.

## Dar de alta una clínica

Con el plan actual, la API de GHL no permite crear subcuentas: requiere Agency Pro. Por eso el paso 0 (`crear_subcuenta.py`) completa el formulario del panel en el navegador. Usa un Brave ya abierto y logueado en GHL, así no maneja credenciales:

```bash
brave-browser --remote-debugging-port=9222          # e iniciá sesión en GHL
.venv/bin/python crear_subcuenta.py <slug> --alta   # crea la subcuenta y corre el alta completa
```

`crear_subcuenta.py`:
- Si ya existe una subcuenta con el nombre de la clínica, la reutiliza.
- Si no, la crea con el snapshot "Blank Snapshot" (se cambia con `--snapshot`) y toma el `locationId` de la URL.
- Espera a que se instale la app de Marketplace.
- Con `--alta`, sigue con `alta.py`. Si algo falla, guarda una captura en `error_crear_subcuenta.png`.

Si la subcuenta ya está creada, el alta se puede correr sola:

```bash
.venv/bin/python alta.py <slug> --location <locationId> --dry-run   # ver qué haría
.venv/bin/python alta.py <slug> --location <locationId>
```

Es idempotente: si lo corrés de nuevo, actualiza lo que ya existe. El agente de Retell se publica en una versión nueva y se borran las anteriores. Retell no deja borrar la versión 0.

### Pasos en GHL

| Paso | Qué hace | Token |
|---|---|---|
| 0 | Completa el perfil (nombre, dirección, teléfono, email, zona horaria) | Agencia |
| 1 | Crea el usuario del profesional | Subcuenta |
| 2 | Crea el calendario con horario, duración del turno y el profesional | Subcuenta |
| 2a | Iguala el horario de trabajo del profesional al de la clínica y lo vincula al calendario. Sin esto, GHL solo ofrece turnos de lunes a viernes | Subcuenta |
| 2b | Desactiva el calendario de ejemplo de GHL | Subcuenta |
| 3 | Crea los custom values (dirección, teléfono, horario, profesional) | Subcuenta |
| 4 / 4b | Notificaciones por email al paciente y al profesional. Se crean `booked` y `confirmation`; esta última es la que se dispara, porque el calendario confirma solo | Subcuenta |
| 5 | Crea el embudo "Pacientes" con sus etapas | Subcuenta |
| 6 | Crea el menú "Llamadas del agente" en la barra lateral (iframe a la página de llamadas) | Agencia |

El token de subcuenta lo pide `ghl_oauth.py` con la app de Marketplace, así que no hace falta una clave por subcuenta. Los pasos 0 y 6 usan la integración privada de agencia, porque la app no tiene esos permisos.

## Correr el middleware

```bash
cd middleware && ../.venv/bin/uvicorn app:app --port 8000
ngrok http 8000 --url <MIDDLEWARE_URL>
```

| Endpoint | Para qué |
|---|---|
| `POST /tools/{nombre}` | Herramientas del agente: `buscar_o_crear_cliente`, `consultar_disponibilidad`, `agendar_cita`, `enviar_correo` |
| `POST /retell/webhook` | Fin de llamada (`call_analyzed`): guarda en el contacto de GHL una nota con el resumen y el link a la llamada |
| `GET /llamadas` | Lista de llamadas de una clínica. Es la página que abre el menú de GHL |
| `GET /llamadas/{call_id}` | Transcripción y grabación de una llamada, leídas de Retell en el momento |
| `GET /salud` | Para comprobar que está vivo |

**Seguridad:**
- Cada clínica tiene un secreto propio, que el alta pone en las herramientas del agente (header `X-Middleware-Token`) y en la URL del webhook. El middleware también acepta la firma de Retell.
- Las páginas de llamadas exigen otro token por clínica, que va en la URL del menú de GHL.

Para probar un agente: con el middleware y ngrok corriendo, entrá a Retell → Agents → el agente de la clínica → Test.

## Limitaciones conocidas (demo)

- Las subcuentas de GHL se crean desde el navegador (la API requiere Agency Pro). Si GHL cambia su panel, hay que ajustar `crear_subcuenta.py`.
- Los correos salen del dominio compartido de GHL y suelen llegar a spam.
- El middleware corre en local: si ngrok o la máquina se apagan, el agente no puede usar sus herramientas. El prompt le indica tomar los datos y avisar que la clínica va a llamar.
- Sin Twilio en la demo (requiere cuenta paga): el agente se prueba con llamadas web y pide el teléfono al paciente. Cómo se conectaría un número por SIP Trunk y cómo se automatizaría para 50 clínicas: [`docs/telefonia-twilio-retell.md`](docs/telefonia-twilio-retell.md).
- La transcripción y la grabación quedan en Retell. Por defecto el link del audio no vence.
