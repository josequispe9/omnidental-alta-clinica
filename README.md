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
alta.py                  # alta completa de una clínica (GHL + Retell + registro)
clinicas.example.json    # ejemplo de datos de clínicas (copiar a clinicas.json)
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

Archivos locales que **no** se suben: `.env`, `clinicas.json`, `registro.json` (IDs y secretos de cada clínica), `llamadas.json`, `ghl-alta/.ghl_oauth.json` (tokens OAuth) y los logs.

## Configuración (una sola vez)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                       # completar los valores
cp clinicas.example.json clinicas.json     # cargar los datos reales de las clínicas
cd ghl-alta && ../.venv/bin/python ghl_oauth.py instalar   # instala la app de Marketplace en la agencia
```

Al instalar la app, elegí la agencia y marcá que se instale en todas las subcuentas, también las futuras.

## Dar de alta una clínica

1. Creá la subcuenta a mano en GHL. Con el plan actual la API no lo permite: requiere Agency Pro.
2. Confirmá que la app quedó instalada: `cd ghl-alta && ../.venv/bin/python ghl_oauth.py subcuentas`.
3. Corré el alta:

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

- Las subcuentas de GHL se crean a mano: la API requiere Agency Pro.
- Los correos salen del dominio compartido de GHL y suelen llegar a spam.
- El middleware corre en local: si ngrok o la máquina se apagan, el agente no puede usar sus herramientas. El prompt le indica tomar los datos y avisar que la clínica va a llamar.
- Sin Twilio todavía: el agente se prueba con llamadas web y pide el teléfono al paciente.
- La transcripción y la grabación quedan en Retell. Por defecto el link del audio no vence.
