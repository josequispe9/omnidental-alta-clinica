# Plan: alta de clínicas (GHL + Retell + middleware)

Basado en `instrucciones-alta-clinica.txt`. Cada etapa termina con una prueba concreta antes de pasar a la siguiente.

## Estructura del proyecto

```
clinicas.json              # datos de las 3 clínicas simuladas
registro.json              # lo completa el alta: locationId -> agent_id, llm_id, calendar_id, pipeline, etapas
alta.py                    # orquesta el alta: GHL + Retell + registro
ghl-alta/                  # lo que ya existe: alta_ghl.py y ghl_oauth.py (OAuth de Marketplace)
  (middleware/retell_alta.py crea el LLM y el agente de Retell de una clínica)
middleware/
  app.py                   # FastAPI: tools, webhook de Retell y página /llamadas
  ghl.py                   # funciones de GHL (contacto, disponibilidad, cita, correo, oportunidad, nota)
  retell.py                # firma de Retell y lectura de llamadas
  prompt.py                # arma el prompt del agente a partir de la clínica
```

## Etapa 0: preparación
- Instalar `fastapi` y `uvicorn`. `requests` ya está instalado.
- ngrok:
  - autenticar la cuenta (`ngrok config add-authtoken …`, lo hacés vos)
  - reservar el dominio fijo gratuito
  - guardarlo en `.env` como `MIDDLEWARE_URL`
- Crear `clinicas.json` con 3 clínicas y todos los campos de la sección 1.
- **Prueba:** `ngrok http 8000 --url <dominio>` responde desde internet.

## Etapa 1: alta en GHL (adaptar lo que ya existe)
- `alta_ghl.py` ya hace perfil, profesional, calendario, notificaciones y embudo. Hay que adaptarlo para que:
  - lea una clínica de `clinicas.json` (se elige por slug)
  - devuelva los IDs para el registro
- Vos creás a mano 3 subcuentas en GHL y me pasás sus IDs.
- Confirmar que la app de Marketplace quedó instalada en cada una (`python ghl_oauth.py subcuentas`).
- **Prueba:** correr el alta de las 3 clínicas y revisar en GHL que estén el calendario, el embudo y las notificaciones.

## Etapa 2: middleware, lado GHL (sin Retell todavía)
- `middleware/ghl.py`:
  - `buscar_o_crear_cliente`: `POST /contacts/upsert`
  - `consultar_disponibilidad`: `GET /calendars/{id}/free-slots`, en la zona horaria de la clínica; si no hay lugar, devuelve los horarios libres más cercanos
  - `agendar_cita`: crea la cita y además la oportunidad en la etapa "Turno agendado"
  - `enviar_correo`: `POST /conversations/messages`
  - `agregar_nota`
- **Prueba:** un script que llama cada función contra una clínica de prueba y después borra lo que creó.

## Etapa 3: agente de Retell
- `prompt.py` arma el prompt con los datos, horarios, servicios, FAQ, reglas de urgencia y la fecha de hoy.
- `retell_alta.py`:
  - crea el LLM con el prompt y las 4 tools (cada una apunta a `MIDDLEWARE_URL/tools/<nombre>`)
  - crea el agente con voz, idioma, saludo, el webhook `MIDDLEWARE_URL/retell/webhook` y el análisis post-llamada
  - publica el agente
  - es idempotente: si la clínica ya tiene un agente en el registro, lo actualiza en vez de crear otro
- **Prueba:** el agente aparece en el panel de Retell con el prompt correcto.

## Etapa 4: middleware, lado Retell
- `app.py`:
  - `POST /tools/{nombre}`:
    - verifica la firma de Retell
    - busca la clínica por `agent_id` en el registro
    - llama la función de GHL
    - devuelve un texto corto que el agente pueda decir
  - `POST /retell/webhook`: con el evento `call_analyzed`, guarda en el contacto una nota con el resumen y el link a la llamada.
- **Prueba de punta a punta:** una web call desde el panel de Retell.
  - Pedís un turno y el agente revisa la disponibilidad, crea el cliente y agenda.
  - En GHL aparecen el contacto, la cita, la oportunidad y la nota, y llegan los emails.

## Etapa 5: ver las llamadas dentro de GHL
- `GET /llamadas?location=<id>&token=…` lista las llamadas de esa clínica, consultando a Retell en el momento.
- `GET /llamadas/<call_id>` muestra la transcripción y la grabación, también consultando a Retell.
- Para el token, alcanza con uno por clínica, guardado en el registro.
- Agregar el Custom Menu Link en modo iframe en cada subcuenta. Primero averiguo si se puede crear por API; si no, se agrega a mano y el alta te muestra la URL exacta a pegar.
- **Prueba:** desde GHL, abrir el menú y ver la transcripción de la llamada de la etapa 4.

## Etapa 6: orquestador
- `python alta.py <slug> --location <ID>` corre en orden:
  1. el alta en GHL
  2. el agente de Retell
  3. el guardado en el registro
  4. al final muestra el link del Custom Menu Link y cómo probar el agente
- **Prueba:** borrar una de las 3 clínicas de prueba y darla de alta desde cero con un solo comando.

## Riesgos conocidos
- Los correos de GHL llegan a spam por el dominio compartido: alcanza para la demo.
- Si ngrok se cae, el agente no puede usar las tools. Conviene que el prompt le diga qué hacer en ese caso: tomar los datos y avisar que la clínica va a llamar.
- No está confirmado que GHL permita el iframe del Custom Menu Link con una URL de ngrok. Si no lo permite, se abre en otra pestaña.
- Sin Twilio, el agente tiene que pedir el teléfono del paciente.

## Lo que necesito de vos
- Etapa 0: autenticar ngrok y reservar el dominio.
- Etapa 1: crear las 3 subcuentas y pasarme los IDs, o empezar con 1 y sumar las otras 2 al final.
