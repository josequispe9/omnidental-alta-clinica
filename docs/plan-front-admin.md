# Plan: panel de administración para el alta de clínicas

> Implementado. Después se pasó todo a una base de datos, hoy Postgres en Supabase (ver `datos.py`): donde este plan dice `clinicas.json`, `registro.json` o `admin.db`, hoy es esa base.

## Qué tiene que hacer
1. Pegar varias webs de clínicas y encolarlas para **extraer** los datos (`extraer_web.py`).
2. Mostrar los datos extraídos de cada clínica en un formulario, para **revisarlos y completar** lo que falta.
3. Al validar una clínica, encolar el **alta**:
   - crear la subcuenta de GHL (`crear_subcuenta.py`, en Brave);
   - configurar la subcuenta y crear el agente de Retell (`alta.py`).
4. Todo **en cola**: una clínica a la vez y, cuando termina, empieza la siguiente.
5. Un **registro de actividad** que muestre en qué etapa está cada clínica, qué se está haciendo y qué falló.

## Decisiones de diseño

| Tema | Propuesta | Por qué |
|---|---|---|
| Servidor | Una app FastAPI aparte (`admin/`), en `127.0.0.1:8001` | El middleware está publicado en internet por ngrok. El panel crea subcuentas y gasta créditos, así que solo debe abrirse desde tu máquina. |
| Front | Una sola página HTML con JavaScript, sin compilar nada | Alcanza para una demo y no suma Node ni dependencias. |
| Colas | **Dos colas, cada una de a una tarea**: extracción y alta | La extracción no necesita a nadie y puede avanzar mientras revisás otra clínica. El alta es estrictamente de a una, porque usa Brave y escribe `registro.json`. |
| Estado | SQLite (`admin.db`, viene con Python) | Sobrevive a un reinicio. Si se corta algo, la tarea queda como "interrumpida" y se puede reintentar. |
| Cómo corren los scripts | Como procesos aparte, los mismos de la terminal | Los scripts terminan con `sys.exit` ante un error. En otro proceso eso no tira abajo el panel, y la terminal sigue funcionando igual. |
| Registro | Los scripts emiten **eventos JSON**, uno por línea, cuando corren desde el panel | El panel sabe la etapa y el paso exactos sin interpretar texto libre. |
| Actualización | El front pregunta cada 2 segundos | Es lo más simple y no pierde nada si se recarga la página. |
| Datos para los scripts | Al validar, la clínica se escribe en `clinicas.json` | Los scripts no cambian: siguen leyendo `clinicas.json` por slug. |

## Estados de una clínica

```
Extracción:  en cola → extrayendo → por revisar
                                  ↘ error (reintentar)
Alta:        validada → en cola → creando subcuenta → configurando GHL → creando agente → lista
                                  ↘ error en <etapa> (reintentar)
```

- Una web con **varias sedes** genera una clínica por sede. Cada una se revisa y se da de alta por separado, y se puede descartar una sede.
- **Reintentar** es seguro:
  - `crear_subcuenta.py` reutiliza la subcuenta si ya existe una con ese nombre;
  - `alta.py` actualiza lo que ya está creado.

## Registro de actividad

Módulo nuevo `progreso.py`, que usan los tres scripts:

```python
evento(etapa="ghl", paso="2a", total=9, mensaje="Igualar el horario de trabajo del profesional", nivel="info")
```

- **En la terminal** imprime lo mismo que hoy.
- **Desde el panel** (`ALTA_EVENTOS=json`) imprime una línea JSON. El panel la guarda en la tabla `eventos` y calcula el avance.

Etapas y pasos:

| Etapa | Pasos |
|---|---|
| `extraccion` | descargar (página i de N) → datos seguros → Claude → borrador |
| `subcuenta` | conectar con Brave → snapshot → formulario → crear → cambiar a la subcuenta → esperar la app de Marketplace |
| `ghl` | pasos 0 a 6 del alta |
| `retell` | R0 a R4 |

Cada evento lleva hora, nivel (info, aviso o error) y mensaje. Si hay un error, se guarda la línea final del script y, en el paso del navegador, la captura `error_crear_subcuenta.png`, que el panel muestra.

## Pantallas

**1. Clínicas (inicio)**
- Un cuadro para pegar varias URLs, una por línea, y un botón "Extraer".
- Una tabla con cada clínica:
  - web y nombre
  - estado, con barra de avance
  - etapa y paso actual
  - acciones: revisar, reintentar, descartar
- Arriba, el **estado del sistema**:
  - si Brave está abierto en el puerto 9222 y con la sesión de GHL iniciada;
  - si el middleware y ngrok responden;
  - si están las claves en `.env`;
  - el largo de cada cola.

**2. Revisión de una clínica**
- **Panel lateral:** los datos que faltan, las contradicciones (con links a las páginas) y las observaciones.
- **Formulario por secciones:**
  - clínica y perfil
  - contacto de la subcuenta
  - profesional
  - horario: una grilla por día con tramos que se agregan y se quitan
  - duración del turno
  - servicios y precios
  - preguntas frecuentes
  - equipo
  - información útil
  - urgencias
  - agente: nombre, idioma, voz y saludo
- Los campos `COMPLETAR` se marcan en rojo, y cada dato muestra un link a la página de donde salió.
- **Botones:**
  - "Guardar borrador"
  - "Ver prompt del agente" (vista previa con `armar_prompt`)
  - "Validar y dar de alta"
- **El servidor valida antes de encolar:**
  - que no quede nada en `COMPLETAR`;
  - emails y horas bien escritos;
  - zona horaria válida;
  - que el slug no esté repetido y la clínica no esté dada de alta.

**3. Detalle del alta**
- La línea de tiempo de eventos, con el paso actual destacado.
- Al terminar, los links:
  - a la subcuenta de GHL;
  - al agente de Retell;
  - a la página de llamadas.

## API del panel

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | La página |
| GET | `/api/estado` | Estado del sistema y de las colas |
| POST | `/api/extracciones` | `{urls: [...]}`: encola las extracciones |
| GET | `/api/clinicas` | Lista con estado y avance |
| GET | `/api/clinicas/{id}` | Datos, borrador, revisión y fuentes |
| PUT | `/api/clinicas/{id}` | Guarda lo editado |
| GET | `/api/clinicas/{id}/prompt` | Vista previa del prompt |
| POST | `/api/clinicas/{id}/validar` | Valida, escribe en `clinicas.json` y encola el alta |
| POST | `/api/clinicas/{id}/reintentar` | Vuelve a encolar desde la etapa que falló |
| DELETE | `/api/clinicas/{id}` | Descarta una clínica, o la saca de la cola si todavía no empezó |
| GET | `/api/clinicas/{id}/eventos?desde=N` | Registro incremental |

## Estructura

```
admin/
  app.py          # FastAPI: API y página
  colas.py        # dos hilos de trabajo; corre los scripts y guarda sus eventos
  db.py           # SQLite: tablas clinicas y eventos
  validar.py      # reglas antes de encolar el alta
  static/index.html, app.js, estilos.css
progreso.py       # eventos para terminal o JSON (lo usan los scripts)
```

## Etapas de trabajo

Cada etapa termina con una prueba.

1. **Eventos en los scripts.**
   - Crear `progreso.py` y usarlo en `extraer_web.py`, `crear_subcuenta.py`, `alta.py`, `alta_ghl.py` y `retell_alta.py`.
   - Agregar al extractor la opción `--salida <archivo>`, para que el panel sepa dónde quedó el borrador.
   - **Prueba:** desde la terminal, la salida es la misma de hoy; con `ALTA_EVENTOS=json` salen líneas JSON válidas.
2. **Servidor y colas.**
   - SQLite, las dos colas, la ejecución de los scripts y la API.
   - Al arrancar, las tareas que estaban en curso quedan como "interrumpidas".
   - **Prueba:** encolar 2 webs con `curl`; se extraen de a una y quedan "por revisar".
3. **Pantalla de inicio**: carga de URLs, tabla con avance y estado del sistema.
   - **Prueba:** ver avanzar 3 extracciones en vivo.
4. **Pantalla de revisión**: formulario, horario por día, vista previa del prompt y validación.
   - **Prueba:**
     - editar un borrador y guardarlo;
     - ver que no se puede validar si quedan campos en `COMPLETAR`;
     - validar y ver la entrada nueva en `clinicas.json`.
5. **Cola de alta y detalle**: subcuenta + GHL + Retell, de a una, con su línea de tiempo y los links al terminar.
   - **Prueba de punta a punta** con 2 webs:
     - las 2 subcuentas se crean una después de la otra;
     - una llamada de prueba a uno de los agentes;
     - después se borran las subcuentas y los agentes de prueba.
6. **Errores y documentación**: reintentar, descartar, mostrar la captura del error y actualizar el README.

## Fuera de alcance (demo)
- Usuarios y contraseñas: el panel solo se abre desde tu máquina.
- Editar o dar de baja clínicas que ya están dadas de alta. Para cambiar algo, se edita `clinicas.json` y se corre `alta.py`.
- Varias altas a la vez: hay un solo Brave y un solo `registro.json`.
